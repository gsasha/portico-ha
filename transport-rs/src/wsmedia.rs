//! Port of `internal/wsmedia` (`codec.go`, `leg.go`) and `cmd/portico-transport/wsmedia.go`
//! (DEV-764 slice 8): the away phone's audio carried as frames on the signalling WebSocket
//! (`media_open` / `media_frame` / `media_close` in, `media_frame` / `media_closed` out).
//!
//! A [`Leg`] has the shape of an `sfu::Session` as far as `internal/call` is concerned
//! ([`ClientLeg`]), so [`WsMediaHub::open`] joins it to a device leg with the code that joins a
//! browser (`joinCallToBrowser`, `crate::webrtc::join_call_to_browser_with`). Frames are 40 ms in
//! one of the codecs the SFU and the relay already use: `pcmu` (8 kHz mu-law, 320 bytes) or `l16`
//! (16 kHz little-endian PCM, 1280 bytes); both channels towards the device carry `l16`.
//!
//! The mu-law and resampling code is `crate::sfu::{g711, resample}` (Go copies it into this
//! package so that it does not import pion; the golden vectors of `tests/golden_wsmedia.rs` check
//! the copy against the real `wsmedia.Leg`).
//!
//! Rust-specific shape: the callbacks of a closing leg are futures, run in registration order on
//! one task that the closer waits for (so a closer torn down mid-way cannot lose the callbacks it
//! had not reached), each under a panic guard so that one that panics cannot stop the rest; the
//! downlink `send` is a future too (a notification written to the IPC socket is awaited, as Go's
//! blocking `Notify` is), guarded the same way. PARITY.md 156 to 161 has the reasons.

use crate::util::{base64_decode, base64_encode, lock};
use std::collections::HashMap;
use std::future::Future;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::time::Instant;

use serde_json::{json, Value};
use tokio::sync::{mpsc, Mutex as AsyncMutex, Notify};

use crate::call::ClientLeg;
use crate::cancel::CancelToken;
use crate::gojson::{self, Kind, Spec};
use crate::ipc::{raw_str, HandlerResult, IpcError, Params, Server};
use crate::sfu::resample::{g711_frame_to_l16, l16_frame_to_g711};

pub const CODEC_PCMU: &str = "pcmu";
pub const CODEC_L16: &str = "l16";

pub const FRAME_MILLIS: i64 = 40;
/// 640 samples at 16 kHz, 2 bytes each: `sip.RTPFrameBytes`.
pub const L16_FRAME_BYTES: usize = 1280;
/// 320 samples at 8 kHz, 1 byte each.
pub const PCMU_FRAME_BYTES: usize = 320;
pub const REASON_IDLE: &str = "idle";

/// How long a phone's audio channel may go without an uplink frame before the leg closes
/// (`wsMediaIdle`): a vanished phone must not hold the call.
pub const WS_MEDIA_IDLE: Duration = Duration::from_secs(5);

/// Go's `make(chan []byte, 8)` on both audio channels.
const CHAN_CAP: usize = 8;
/// Legs open at once; HA holds a few calls at a time (DEV-796 C2).
pub const MAX_LEGS: usize = 16;
/// The longest `d` worth decoding: a frame is at most 1280 bytes (1708 in base64); the slack
/// allows line breaks the decoder skips (DEV-796 C6).
pub(crate) const MAX_FRAME_B64: usize = 4096;

/// Upper bounds (ms) of the uplink inter-arrival histogram; frames come every 40 ms. The last
/// bin is open-ended (its p95 reads the observed maximum).
pub const GAP_BOUNDS_MS: [u64; 8] = [45, 60, 80, 120, 200, 400, 800, 2000];

/// DEV-811 P3: the numbers of one frames leg, counted only when the `media_open` that created
/// it said `stats: true` (otherwise every hook is one relaxed load and nothing is written).
#[derive(Default)]
struct LegStats {
    enabled: AtomicBool,
    uplink_frames: AtomicU64,
    uplink_bad_frame: AtomicU64,
    uplink_full_drops: AtomicU64,
    downlink_frames: AtomicU64,
    downlink_send_ms_max: AtomicU64,
    gap_ms_max: AtomicU64,
    gap_bins: [AtomicU64; 9],
    last_uplink: Mutex<Option<Instant>>,
}

impl LegStats {
    fn on(&self) -> bool {
        self.enabled.load(Ordering::Relaxed)
    }

    /// An accepted uplink frame arrived at `now`: the gap since the previous one.
    fn uplink_arrival(&self, now: Instant) {
        self.uplink_frames.fetch_add(1, Ordering::Relaxed);
        let prev = lock(&self.last_uplink).replace(now);
        if let Some(prev) = prev {
            let ms = u64::try_from(now.saturating_duration_since(prev).as_millis()).unwrap_or(u64::MAX);
            self.gap_ms_max.fetch_max(ms, Ordering::Relaxed);
            let bin = GAP_BOUNDS_MS
                .iter()
                .position(|b| ms <= *b)
                .unwrap_or(GAP_BOUNDS_MS.len());
            self.gap_bins[bin].fetch_add(1, Ordering::Relaxed);
        }
    }

    /// The `stats` object of `media_closed`.
    fn snapshot(&self) -> Value {
        let g = |c: &AtomicU64| c.load(Ordering::Relaxed);
        let bins: Vec<u64> = self.gap_bins.iter().map(g).collect();
        let total: u64 = bins.iter().sum();
        let max = g(&self.gap_ms_max);
        // p95: the upper bound of the bin where the cumulative count reaches 95 % of the gaps
        // (the observed maximum for the open-ended bin); 0 when the call had no gap at all.
        let p95 = if total == 0 {
            0
        } else {
            let need = (total * 95).div_ceil(100);
            let mut cum = 0;
            let mut out = max;
            for (i, n) in bins.iter().enumerate() {
                cum += n;
                if cum >= need {
                    out = GAP_BOUNDS_MS.get(i).copied().map_or(max, |b| b.min(max));
                    break;
                }
            }
            out
        };
        json!({
            "uplink_frames": g(&self.uplink_frames),
            "uplink_bad_frame": g(&self.uplink_bad_frame),
            "uplink_full_drops": g(&self.uplink_full_drops),
            "downlink_frames": g(&self.downlink_frames),
            "downlink_send_ms_max": g(&self.downlink_send_ms_max),
            "uplink_gap_ms_max": max,
            "uplink_gap_ms_p95": p95,
            // the per-call mic arrival gap histogram: counts per bin of GAP_BOUNDS_MS, last bin open-ended
            "uplink_gap_bins": bins,
        })
    }
}

type BoxFut = Pin<Box<dyn Future<Output = ()> + Send>>;
/// What the leg calls with each encoded frame for the phone (from one task, in order).
pub type SendFn = Arc<dyn Fn(Vec<u8>) -> BoxFut + Send + Sync>;
type ClosedCb = Box<dyn FnOnce(String) -> BoxFut + Send>;

pub use crate::util::PushError;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Codec {
    Pcmu,
    L16,
}

enum Cb {
    /// a `ClientLeg::on_disconnected` callback
    Sync(Box<dyn FnOnce() + Send>),
    Async(ClosedCb),
}

struct State {
    closed: bool,
    reason: String,
    on_closed: Vec<Cb>,
}

/// One phone's audio channel for one call (`wsmedia.Leg`).
pub struct Leg {
    codec: Codec,
    in_tx: mpsc::Sender<Vec<u8>>,
    out_tx: mpsc::Sender<Vec<u8>>,
    out_rx: Arc<AsyncMutex<mpsc::Receiver<Vec<u8>>>>,
    stop: CancelToken,
    /// restarts the idle clock (`timer.Reset`)
    reset: Notify,
    st: Mutex<State>,
    /// the downlink's receiver once the leg is closed: Go's `l.in` stays a buffered channel that
    /// accepts (and never reads) up to 8 more frames; a `try_send` on a dropped receiver would
    /// report an error instead (PARITY.md 159)
    parked: Mutex<Option<mpsc::Receiver<Vec<u8>>>>,
    stats: LegStats,
}

/// Runs `f` under a panic guard: a panic in it is logged and swallowed, so the callbacks after
/// it still run (no task of its own, `crate::guard`).
async fn run_guarded(what: &str, f: BoxFut) {
    crate::guard::logged(what, f).await;
}

impl Leg {
    /// `New`: opens a leg. `send` is called (from one task, in order) with each encoded frame for
    /// the phone. `idle` is how long without an uplink frame ends the leg; the clock starts now.
    /// Needs a tokio runtime (the idle clock and the downlink are tasks).
    pub fn new(codec: &str, idle: Duration, send: SendFn) -> Result<Arc<Leg>, String> {
        let codec = match codec {
            CODEC_PCMU => Codec::Pcmu,
            CODEC_L16 => Codec::L16,
            other => {
                return Err(format!(
                    "wsmedia: unsupported codec {}",
                    crate::gostd::strconv::go_quote(other)
                ))
            }
        };
        // Must be called inside the runtime, as `house::Manager::new` and `open` must be (R1-199):
        // outside one `Handle::current` panics, it is a caller bug and never a `bad_params`.
        let rt = tokio::runtime::Handle::current();
        let (in_tx, in_rx) = mpsc::channel(CHAN_CAP);
        let (out_tx, out_rx) = mpsc::channel(CHAN_CAP);
        let leg = Arc::new(Leg {
            codec,
            in_tx,
            out_tx,
            out_rx: Arc::new(AsyncMutex::new(out_rx)),
            stop: CancelToken::new(),
            reset: Notify::new(),
            st: Mutex::new(State {
                closed: false,
                reason: String::new(),
                on_closed: Vec::new(),
            }),
            parked: Mutex::new(None),
            stats: LegStats::default(),
        });
        let l = leg.clone();
        rt.spawn(async move {
            loop {
                tokio::select! {
                    biased;
                    _ = l.stop.cancelled() => return,
                    _ = l.reset.notified() => {}
                    _ = tokio::time::sleep(idle) => {
                        l.close(REASON_IDLE).await;
                        return;
                    }
                }
            }
        });
        rt.spawn(downlink(leg.clone(), in_rx, codec, send));
        Ok(leg)
    }

    fn st(&self) -> std::sync::MutexGuard<'_, State> {
        lock(&self.st)
    }

    /// DEV-811 P3: count this leg (called right after `new`, before the first frame).
    pub fn enable_stats(&self) {
        self.stats.enabled.store(true, Ordering::Relaxed);
    }

    /// The counters as the `stats` object of `media_closed`; `None` while counting is off.
    pub fn stats_snapshot(&self) -> Option<Value> {
        self.stats.on().then(|| self.stats.snapshot())
    }

    /// `OnClosed`: `cb` runs once, when the leg is closed, with the reason. Registered after the
    /// fact, it runs at once (before this returns).
    pub async fn on_closed(&self, cb: impl FnOnce(String) -> BoxFut + Send + 'static) {
        let reason = {
            let mut g = self.st();
            if !g.closed {
                g.on_closed.push(Cb::Async(Box::new(cb)));
                return;
            }
            g.reason.clone()
        };
        run_guarded("wsmedia late close callback", cb(reason)).await;
    }

    /// `OnDisconnected`: `OnClosed` without the reason, synchronous (the shape of
    /// [`ClientLeg::on_disconnected`]). A callback registered on a closed leg runs at once, on
    /// the caller's stack.
    fn on_disconnected_sync(&self, cb: Box<dyn FnOnce() + Send>) {
        {
            let mut g = self.st();
            if !g.closed {
                g.on_closed.push(Cb::Sync(cb));
                return;
            }
        }
        if catch_unwind(AssertUnwindSafe(cb)).is_err() {
            crate::panic_hook::out_line(
                "portico-transport: wsmedia: a disconnect callback panicked",
            );
        }
    }

    /// `Push`: takes one uplink frame from the phone. Never blocks: a full channel drops the frame.
    pub fn push(&self, payload: &[u8]) -> Result<(), PushError> {
        {
            let g = self.st();
            if g.closed {
                return Err(PushError::Closed);
            }
            let want = match self.codec {
                Codec::Pcmu => PCMU_FRAME_BYTES,
                Codec::L16 => L16_FRAME_BYTES,
            };
            if payload.len() != want {
                if self.stats.on() {
                    self.stats.uplink_bad_frame.fetch_add(1, Ordering::Relaxed);
                }
                return Err(PushError::BadFrame);
            }
            self.reset.notify_one();
        }
        if self.stats.on() {
            self.stats.uplink_arrival(Instant::now());
        }
        let frame = match self.codec {
            Codec::Pcmu => g711_frame_to_l16(payload),
            Codec::L16 => payload.to_vec(),
        };
        if self.out_tx.try_send(frame).is_err() && self.stats.on() {
            self.stats.uplink_full_drops.fetch_add(1, Ordering::Relaxed);
        }
        Ok(())
    }

    /// `Close`: ends the leg once; later calls do nothing (they return at once, even while the
    /// first call's callbacks are still running). The callbacks run in registration order.
    ///
    /// The callbacks run on a task of their own that the caller only waits for (R1-34): a caller
    /// dropped in the middle (a house tail torn down) cannot lose the callbacks it had not reached,
    /// because a later `close` returns at once and would never run them.
    pub async fn close(&self, reason: &str) {
        let cbs = {
            let mut g = self.st();
            if g.closed {
                return;
            }
            g.closed = true;
            g.reason = reason.to_string();
            std::mem::take(&mut g.on_closed)
        };
        self.stop.cancel();
        let reason = reason.to_string();
        let run = async move {
            for cb in cbs {
                let fut: BoxFut = match cb {
                    Cb::Sync(f) => Box::pin(async move { f() }),
                    Cb::Async(f) => f(reason.clone()),
                };
                run_guarded("wsmedia close", fut).await;
            }
        };
        // Dropping the handle (the caller went away) does not abort the task.
        let _ = tokio::spawn(run).await;
    }

    pub fn is_closed(&self) -> bool {
        self.st().closed
    }
}

/// `downlink`: frames from the device (l16) to the phone, encoded for its codec.
async fn downlink(leg: Arc<Leg>, mut rx: mpsc::Receiver<Vec<u8>>, codec: Codec, send: SendFn) {
    loop {
        let frame = tokio::select! {
            biased;
            _ = leg.stop.cancelled() => break,
            f = rx.recv() => match f {
                Some(f) => f,
                None => break,
            },
        };
        if leg.stop.is_cancelled() || frame.len() != L16_FRAME_BYTES {
            continue;
        }
        let frame = match codec {
            Codec::Pcmu => l16_frame_to_g711(&frame),
            Codec::L16 => frame,
        };
        // A panic in `send` (a notification that cannot be built) must not kill this task
        // silently while the uplink keeps the idle clock quiet: close the leg with reason
        // `error` (a fixed text is logged, never the payload) so Python hears `media_closed`.
        // One poll guard, not a task per frame (R1-47).
        // (the call of `send` itself may panic, so it is inside the guarded future)
        let timed = leg.stats.on().then(Instant::now);
        let sent = crate::guard::catch(async { send(frame).await }).await;
        if let Some(t0) = timed {
            leg.stats.downlink_frames.fetch_add(1, Ordering::Relaxed);
            let ms = u64::try_from(t0.elapsed().as_millis()).unwrap_or(u64::MAX);
            leg.stats.downlink_send_ms_max.fetch_max(ms, Ordering::Relaxed);
        }
        if sent.is_err() {
            crate::panic_hook::out_line("portico-transport: wsmedia: sending a frame to the phone failed; the leg is closed");
            leg.close("error").await;
            break;
        }
    }
    *lock(&leg.parked) = Some(rx);
}

impl ClientLeg for Leg {
    /// The WebSocket that carries the leg is already up, which is the event `internal/call`
    /// waits for before it answers the device: `cb` runs at once.
    fn on_connected(&self, cb: Box<dyn FnOnce() + Send>) {
        cb();
    }
    fn on_disconnected(&self, cb: Box<dyn FnOnce() + Send>) {
        self.on_disconnected_sync(cb);
    }
    fn input(&self) -> mpsc::Sender<Vec<u8>> {
        self.in_tx.clone()
    }
    fn output(&self) -> Arc<AsyncMutex<mpsc::Receiver<Vec<u8>>>> {
        self.out_rx.clone()
    }
}

// ---- the hub ---------------------------------------------------------------------------------------

pub(crate) const WSMEDIA_PARAMS: [Spec; 4] = [
    Spec {
        json: "call_id",
        kind: Kind::Str,
    },
    Spec {
        json: "codec",
        kind: Kind::Str,
    },
    Spec {
        json: "d",
        kind: Kind::Str,
    },
    Spec {
        json: "reason",
        kind: Kind::Str,
    },
];

fn bad_params(m: impl Into<String>) -> IpcError {
    IpcError::new("bad_params", m)
}

/// Where the hub's notifications go (`wsMediaHub.notify`): the IPC server in production.
pub trait MediaNotifier: Send + Sync + 'static {
    fn notify(&self, method: &str, params: Value) -> BoxFut;
}

impl MediaNotifier for Server {
    fn notify(&self, method: &str, params: Value) -> BoxFut {
        let (s, m) = (self.clone(), method.to_string());
        Box::pin(async move {
            let _ = s.notify(&m, &params).await;
        })
    }
}

/// `wsMediaHub.join`: joins the phone's leg to the device leg of its call.
pub type JoinFn = Arc<dyn Fn(&str, Arc<dyn ClientLeg>) + Send + Sync>;

/// The phone-over-WebSocket client legs, one per call id (`wsMediaHub`). Python carries the
/// frames; a leg joins the device leg exactly as a browser's WebRTC session does.
pub struct WsMediaHub {
    legs: Mutex<HashMap<String, Arc<Leg>>>,
    idle: Duration,
    notify: Arc<dyn MediaNotifier>,
    join: JoinFn,
}

impl WsMediaHub {
    pub fn new(notify: Arc<dyn MediaNotifier>, idle: Duration, join: JoinFn) -> Arc<Self> {
        Arc::new(WsMediaHub {
            legs: Mutex::new(HashMap::new()),
            idle,
            notify,
            join,
        })
    }

    fn legs(&self) -> std::sync::MutexGuard<'_, HashMap<String, Arc<Leg>>> {
        lock(&self.legs)
    }

    /// The leg of `call_id` (a test seam: Go's tests read `hub.legs`).
    pub fn leg(&self, call_id: &str) -> Option<Arc<Leg>> {
        self.legs().get(call_id).cloned()
    }

    pub fn len(&self) -> usize {
        self.legs().len()
    }

    /// The call ids in the table (a test seam, R2-77).
    #[cfg(feature = "test-seams")]
    pub fn ids(&self) -> Vec<String> {
        self.legs().keys().cloned().collect()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// `media_open`.
    pub async fn open(self: &Arc<Self>, params: Params) -> HandlerResult {
        const MSG: &str = "media_open needs call_id and codec";
        let v = gojson::decode(raw_str(&params), "wsMediaParams", &WSMEDIA_PARAMS)
            .map_err(|_| bad_params(MSG))?;
        let call_id = gojson::str_at(&v, 0);
        let codec = gojson::str_at(&v, 1);
        if call_id.is_empty() {
            return Err(bad_params(MSG));
        }
        // DEV-811 P3: `stats: true` asks for the leg's counters in `media_closed`. Read apart from
        // the Go-shaped decode above (its field list is the old contract); absent or anything but
        // `true` = off, and an older wheel never looks at the key at all.
        let want_stats = serde_json::from_str::<Value>(raw_str(&params))
            .ok()
            .and_then(|p| p.get("stats").and_then(Value::as_bool))
            == Some(true);
        let leg = {
            let mut legs = self.legs();
            if legs.contains_key(&call_id) {
                return Err(IpcError::new("already_open", call_id));
            }
            if legs.len() >= MAX_LEGS {
                return Err(IpcError::new("too_many", "media_open: too many open legs"));
            }
            let (notify, id) = (self.notify.clone(), call_id.clone());
            let send: SendFn = Arc::new(move |payload: Vec<u8>| {
                notify.notify(
                    "media_frame",
                    json!({"call_id": id, "d": base64_encode(&payload)}),
                )
            });
            let leg = Leg::new(&codec, self.idle, send).map_err(bad_params)?;
            if want_stats {
                leg.enable_stats();
            }
            legs.insert(call_id.clone(), leg.clone());
            leg
        };
        {
            let (hub, id, l) = (self.clone(), call_id.clone(), leg.clone());
            leg.on_closed(move |reason| {
                Box::pin(async move {
                    {
                        let mut legs = hub.legs();
                        if legs.get(&id).is_some_and(|x| Arc::ptr_eq(x, &l)) {
                            legs.remove(&id);
                        }
                    }
                    let mut note = json!({"call_id": id, "reason": reason});
                    if let Some(stats) = l.stats_snapshot() {
                        note["stats"] = stats;
                    }
                    hub.notify.notify("media_closed", note).await;
                })
            })
            .await;
        }
        // The join is the Hub's `joinCallToBrowser`, which only inspects drivers and spawns; a
        // panic in it must not leave a leg that the phone believes joined (PARITY.md 157).
        let as_client: Arc<dyn ClientLeg> = leg.clone();
        let join = self.join.clone();
        let id = call_id.clone();
        if catch_unwind(AssertUnwindSafe(|| join(&id, as_client))).is_err() {
            leg.close("error").await;
            return Err(IpcError::new(
                "internal_error",
                "media_open: joining the call failed",
            ));
        }
        let size = if codec == CODEC_PCMU {
            PCMU_FRAME_BYTES
        } else {
            L16_FRAME_BYTES
        };
        Ok(json!({"codec": codec, "frame_bytes": size, "frame_ms": FRAME_MILLIS}).into())
    }

    /// `media_frame`.
    pub async fn frame(&self, params: Params) -> HandlerResult {
        let v = gojson::decode(raw_str(&params), "wsMediaParams", &WSMEDIA_PARAMS)
            .map_err(bad_params)?;
        let call_id = gojson::str_at(&v, 0);
        let data = gojson::str_at(&v, 2);
        let Some(leg) = self.leg(&call_id) else {
            return Err(IpcError::new("unknown_session", call_id));
        };
        if data.len() > MAX_FRAME_B64 {
            return Err(IpcError::new("bad_frame", PushError::BadFrame.message()));
        }
        let payload = base64_decode(&data).ok_or_else(|| bad_params("d is not base64"))?;
        match leg.push(&payload) {
            Ok(()) => Ok(json!({"ok": true}).into()),
            Err(PushError::BadFrame) => {
                Err(IpcError::new("bad_frame", PushError::BadFrame.message()))
            }
            Err(PushError::Closed) => Err(IpcError::new("unknown_session", call_id)),
        }
    }

    /// `media_close`: closes the leg (Python is told with `media_closed` before the response).
    pub async fn close(&self, params: Params) -> HandlerResult {
        let v = gojson::decode(raw_str(&params), "wsMediaParams", &WSMEDIA_PARAMS)
            .map_err(bad_params)?;
        let call_id = gojson::str_at(&v, 0);
        let mut reason = gojson::str_at(&v, 3);
        if reason.is_empty() {
            reason = "closed".to_string();
        }
        if let Some(leg) = self.leg(&call_id) {
            leg.close(&reason).await;
        }
        Ok(json!({"ok": true}).into())
    }
}
