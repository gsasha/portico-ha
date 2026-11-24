//! `audio.go` plus the `AudioClient` half of `server.go`: the answer path's view of the server
//! (DEV-518 W3). The hub joins a browser to a call by call id; this file lets it do that for the
//! in-process server the way it does for gate_sip_observer's claimed calls:
//!
//! * [`AudioClient`] is the one client that owns an answered call's audio. Frames from the
//!   station arrive through `take_frames()` as PCM16 LE mono; `send()` queues the client's
//!   microphone. The frames channel is CLOSED when the call is over, by any path: that is the
//!   only hang-up notification there is. Both queues hold six frames (120 ms of slack against
//!   bursty delivery) and DROP when full, counting the drop per direction: a client that stops
//!   draining never stalls the RTP path.
//! * [`CallAudioImpl`] dresses an `AudioClient` as a [`CallAudio`]: 16 kHz L16 mono, `output`
//!   in 1280-byte (40 ms) frames, `input` of any even length, the station's microphone raised
//!   x4 (as gate_sip_observer does). `detach()` ends the call at once.
//!
//! What stays in the server: deciding which client is current, what ending a call
//! means, the orphan timer. The server reaches this file through [`ClientHost`]. The Go
//! `Stats` counters that live in `Server.st` are [`AudioStats`] here (atomics instead of
//! `Server.mu`).

use crate::util::lock;
use std::fmt;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Weak};

use serde_json::{json, Value};
use tokio::sync::mpsc;

use crate::cancel::CancelToken;

/// 16 kHz is the rate of the link to the browser (`linkRate`).
pub const LINK_RATE: u32 = 16000;
/// 40 ms of 16 kHz L16: what internal/sfu produces and expects (`linkFrameBytes`).
pub const LINK_FRAME_BYTES: usize = 1280;
/// `linkQueue`: capacity of `In` and `Out`.
pub const LINK_QUEUE: usize = 16;
/// `downlinkGain`: the multiplier on the gate microphone before it goes to the browser (the
/// station's microphone is very quiet). Applied through [`apply_gain_soft`]: linear up to
/// [`LIMITER_KNEE`], then compressed, never a hard clip (the 2026-11-21 frames call clipped
/// 27 % of its frames at x4).
pub const DOWNLINK_GAIN: i64 = 4;
/// The app's microphone is raised for the station's speaker by [`UplinkAgc`], a bounded automatic
/// gain (D-20261121: the tablet's mic peaked at -9 dBFS while its owner was SHOUTING, and the
/// station could hardly hear it). The constants, all in one place:
///
/// * the gain never exceeds this (x8 is +18 dB) and never falls below 1 (the stage only ever
///   raises). **Set it to 1.0 to switch the stage off** (a frame at gain 1 is passed through
///   byte for byte). It raises the room noise and any echo by the same factor.
pub const UPLINK_AGC_MAX_GAIN: f32 = 8.0;
/// The peak the AGC aims a frame at: 16384 is -6 dBFS (a phone talking at -1 dBFS wants a gain
/// below 1 and so is left alone).
pub const UPLINK_AGC_TARGET_PEAK: f32 = 16384.0;
/// A frame whose RMS is below this (-60 dBFS) is the noise floor or silence: the gain is held
/// where it is, never raised on it, so silence stays silent and hiss is not pumped up.
pub const UPLINK_AGC_GATE_RMS: f32 = 33.0;
/// The gain state falls (toward a louder frame's wanted gain) with this time constant.
pub const UPLINK_AGC_ATTACK_S: f32 = 0.1;
/// The gain state rises with this time constant: slow, so a one-second pause that is above the
/// gate (breath, room noise) lifts it by only 28 % of the way to the wanted gain, and a pause
/// below the gate by nothing: it does not pump.
pub const UPLINK_AGC_RELEASE_S: f32 = 3.0;
static LEGACY_AUDIO: AtomicBool = AtomicBool::new(false);

/// Test seam for the Go parity replays (`tests/golden_*.rs`): when set, the downlink uses the hard
/// clamp of Go's `applyGain` and the uplink is passed through at unity, as the Go server does,
/// so the recorded Go traces still compare byte for byte. Never set in production.
pub fn set_legacy_audio(on: bool) {
    LEGACY_AUDIO.store(on, Ordering::Relaxed);
}

fn legacy_audio() -> bool {
    LEGACY_AUDIO.load(Ordering::Relaxed)
}

/// Where [`apply_gain_soft`] starts to compress: 24000 is -2.7 dBFS. Below it the output is
/// exactly `gain x sample`, so the audible gain is unchanged for everything that did not clip.
pub const LIMITER_KNEE: i64 = 24000;
/// The client's two queues (`make(chan []byte, 6)` in `attachFor`).
pub const CLIENT_QUEUE: usize = 6;

/// `ErrCallOver`: the call is over.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CallOver;

impl fmt::Display for CallOver {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("the call is over")
    }
}

impl std::error::Error for CallOver {}

/// The four per-direction counters of `Server.st` that the audio path moves.
#[derive(Debug, Default)]
pub struct AudioStats {
    /// frames queued for the app (`FramesToApp`)
    pub frames_to_app: AtomicI64,
    /// frames the app sent (`FramesFromApp`)
    pub frames_from_app: AtomicI64,
    /// microphone frames dropped because the queue was full (`DroppedUp`)
    pub dropped_up: AtomicI64,
    /// station frames dropped because the app's queue was full (`DroppedDown`)
    pub dropped_down: AtomicI64,
}

impl AudioStats {
    pub fn snapshot(&self) -> (i64, i64, i64, i64) {
        (
            self.frames_to_app.load(Ordering::SeqCst),
            self.frames_from_app.load(Ordering::SeqCst),
            self.dropped_up.load(Ordering::SeqCst),
            self.dropped_down.load(Ordering::SeqCst),
        )
    }
}

/// Bins of the pre-gain peak histogram (DEV-811 P3): 3 dB wide. Bin 0 is everything below
/// -90 dBFS (digital silence included); bin `i` (1..=30) holds peaks in
/// `[-93 + 3i, -90 + 3i)` dBFS, bin 30 also holding full scale. Fine at the top, where the x4
/// gain (+12 dB) starts to clip and where the limiter's constants come from.
pub const PRE_GAIN_BINS: usize = 31;

/// A peak (0..=32768) in the histogram's bins.
pub fn pre_gain_bin(peak: u32) -> usize {
    if peak == 0 {
        return 0;
    }
    let dbfs = 20.0 * (f64::from(peak) / 32768.0).log10();
    let bin = ((dbfs + 90.0) / 3.0).floor() + 1.0;
    if bin < 1.0 {
        0
    } else {
        (bin as usize).min(PRE_GAIN_BINS - 1)
    }
}

/// 20 log10 of an amplitude ratio against full scale, one decimal; -120 for silence.
fn dbfs(x: f64) -> f64 {
    if x <= 0.0 {
        -120.0
    } else {
        (20.0 * (x / 32768.0).log10() * 10.0).round() / 10.0
    }
}

fn rms_dbfs(sumsq: u64, n: u64) -> f64 {
    if n == 0 {
        -120.0
    } else {
        dbfs((sumsq as f64 / n as f64).sqrt())
    }
}

fn samples_of(b: &[u8]) -> impl Iterator<Item = i32> + '_ {
    b.as_chunks::<2>().0.iter().map(|p| i32::from(i16::from_le_bytes([p[0], p[1]])))
}

/// What one direction's gain stage did, cumulative (D-20261121-ee09): the gain, how often the
/// limiter engaged, and the level before and after the gain.
#[derive(Debug, Default)]
pub struct GainMeter {
    /// the last gain, x1000
    gain_milli: AtomicU64,
    /// call-wide lowest/highest gain, x1000 (min starts at `u64::MAX`: none yet)
    gain_min_milli: AtomicU64,
    gain_max_milli: AtomicU64,
    gain_sum_milli: AtomicU64,
    /// the same for the window since the last line was taken
    win_gain_min_milli: AtomicU64,
    win_gain_max_milli: AtomicU64,
    /// frames the AGC held its gain on (noise gate)
    held_frames: AtomicU64,
    frames: AtomicU64,
    limited_frames: AtomicU64,
    samples: AtomicU64,
    limited_samples: AtomicU64,
    sumsq_pre: AtomicU64,
    sumsq_post: AtomicU64,
    peak_post: AtomicU64,
    /// largest pre-/post-gain peak since the last window was taken
    win_peak_pre: AtomicU64,
    win_peak_post: AtomicU64,
}

/// The cumulative numbers of a [`GainMeter`], for differencing.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct MeterSnap {
    pub frames: u64,
    pub limited_frames: u64,
    pub samples: u64,
    pub limited_samples: u64,
    pub sumsq_pre: u64,
    pub sumsq_post: u64,
    pub gain_sum_milli: u64,
    pub held_frames: u64,
}

impl GainMeter {
    fn new() -> Self {
        let m = GainMeter::default();
        m.gain_min_milli.store(u64::MAX, Ordering::Relaxed);
        m.win_gain_min_milli.store(u64::MAX, Ordering::Relaxed);
        m
    }

    /// One frame: `pre` before the gain, `post` after it, `limited` samples the limiter bent.
    pub fn note(&self, gain: f32, held: bool, pre: &[u8], post: &[u8], limited: usize) {
        let gm = (gain * 1000.0).round() as u64;
        self.gain_milli.store(gm, Ordering::Relaxed);
        self.gain_min_milli.fetch_min(gm, Ordering::Relaxed);
        self.gain_max_milli.fetch_max(gm, Ordering::Relaxed);
        self.win_gain_min_milli.fetch_min(gm, Ordering::Relaxed);
        self.win_gain_max_milli.fetch_max(gm, Ordering::Relaxed);
        self.gain_sum_milli.fetch_add(gm, Ordering::Relaxed);
        if held {
            self.held_frames.fetch_add(1, Ordering::Relaxed);
        }
        self.frames.fetch_add(1, Ordering::Relaxed);
        if limited > 0 {
            self.limited_frames.fetch_add(1, Ordering::Relaxed);
            self.limited_samples.fetch_add(limited as u64, Ordering::Relaxed);
        }
        let (mut n, mut sp, mut pp) = (0u64, 0u64, 0u64);
        for v in samples_of(pre) {
            n += 1;
            sp += (i64::from(v) * i64::from(v)) as u64;
            pp = pp.max(u64::from(v.unsigned_abs()));
        }
        let (mut so, mut po) = (0u64, 0u64);
        for v in samples_of(post) {
            so += (i64::from(v) * i64::from(v)) as u64;
            po = po.max(u64::from(v.unsigned_abs()));
        }
        self.samples.fetch_add(n, Ordering::Relaxed);
        self.sumsq_pre.fetch_add(sp, Ordering::Relaxed);
        self.sumsq_post.fetch_add(so, Ordering::Relaxed);
        self.peak_post.fetch_max(po, Ordering::Relaxed);
        self.win_peak_pre.fetch_max(pp, Ordering::Relaxed);
        self.win_peak_post.fetch_max(po, Ordering::Relaxed);
    }

    pub fn snap(&self) -> MeterSnap {
        let g = |c: &AtomicU64| c.load(Ordering::Relaxed);
        MeterSnap {
            frames: g(&self.frames),
            limited_frames: g(&self.limited_frames),
            samples: g(&self.samples),
            limited_samples: g(&self.limited_samples),
            sumsq_pre: g(&self.sumsq_pre),
            sumsq_post: g(&self.sumsq_post),
            gain_sum_milli: g(&self.gain_sum_milli),
            held_frames: g(&self.held_frames),
        }
    }

    /// The call's numbers as stats fields, each key starting `prefix` (`down_` / `up_`).
    fn fields(&self, prefix: &str, out: &mut serde_json::Map<String, Value>) {
        let s = self.snap();
        let ratio = |a: u64, b: u64| if b == 0 { 0.0 } else { (a as f64 / b as f64 * 1e4).round() / 1e4 };
        let mut put = |k: &str, v: Value| {
            out.insert(format!("{prefix}{k}"), v);
        };
        let gx = |c: &AtomicU64| c.load(Ordering::Relaxed) as f64 / 1000.0;
        put("gain", json!(gx(&self.gain_milli)));
        if s.frames > 0 {
            put("gain_min", json!(gx(&self.gain_min_milli)));
            put("gain_max", json!(gx(&self.gain_max_milli)));
            put("gain_avg", json!((s.gain_sum_milli as f64 / s.frames as f64) / 1000.0));
        }
        put("held_frames", json!(s.held_frames));
        put("frames", json!(s.frames));
        put("limited_frames", json!(s.limited_frames));
        put("limited_samples", json!(s.limited_samples));
        put("limited_ratio", json!(ratio(s.limited_frames, s.frames)));
        put("rms_pre_dbfs", json!(rms_dbfs(s.sumsq_pre, s.samples)));
        put("rms_post_dbfs", json!(rms_dbfs(s.sumsq_post, s.samples)));
        put("peak_post_dbfs", json!(dbfs(self.peak_post.load(Ordering::Relaxed) as f64)));
    }

    /// The window `prev..now` as a short text (and starts the next peak window).
    fn window(&self, prev: &MeterSnap) -> String {
        let s = self.snap();
        let dn = s.samples.saturating_sub(prev.samples);
        let df = s.frames.saturating_sub(prev.frames);
        let wmin = self.win_gain_min_milli.swap(u64::MAX, Ordering::Relaxed);
        let wmax = self.win_gain_max_milli.swap(0, Ordering::Relaxed);
        let (gmin, gmax) = if df == 0 { (0.0, 0.0) } else { (wmin as f64 / 1000.0, wmax as f64 / 1000.0) };
        let gavg = if df == 0 {
            0.0
        } else {
            s.gain_sum_milli.saturating_sub(prev.gain_sum_milli) as f64 / df as f64 / 1000.0
        };
        format!(
            "gain_min={gmin} gain_avg={gavg:.2} gain_max={gmax} held_frames=+{} frames=+{} limited_frames=+{} limited_samples=+{} rms_pre_dbfs={} rms_post_dbfs={} peak_pre_dbfs={} peak_post_dbfs={}",
            s.held_frames.saturating_sub(prev.held_frames),
            df,
            s.limited_frames.saturating_sub(prev.limited_frames),
            s.limited_samples.saturating_sub(prev.limited_samples),
            rms_dbfs(s.sumsq_pre.saturating_sub(prev.sumsq_pre), dn),
            rms_dbfs(s.sumsq_post.saturating_sub(prev.sumsq_post), dn),
            dbfs(self.win_peak_pre.swap(0, Ordering::Relaxed) as f64),
            dbfs(self.win_peak_post.swap(0, Ordering::Relaxed) as f64),
        )
    }
}

/// The level at the `q`th fraction of the frames in the pre-gain peak histogram, as the upper
/// bound (dBFS) of the bin holding it; `None` for an empty histogram.
pub fn hist_level_dbfs(hist: &[u64], q: f64) -> Option<i64> {
    let total: u64 = hist.iter().sum();
    if total == 0 {
        return None;
    }
    let need = ((total as f64) * q).ceil().max(1.0) as u64;
    let mut cum = 0;
    for (i, n) in hist.iter().enumerate() {
        cum += n;
        if cum >= need {
            return Some(-90 + 3 * i as i64);
        }
    }
    Some(0)
}

/// The per-call counters of one answered call's audio (DEV-811 P3), the per-call home beside
/// the per-server [`AudioStats`]. Counting is OFF until [`CallCounters::enable`]: with it off
/// every hook is one relaxed boolean load.
#[derive(Debug)]
pub struct CallCounters {
    enabled: AtomicBool,
    /// station frames the downlink saw (any length)
    station_frames: AtomicU64,
    /// pre-gain peak histogram of those frames
    hist: [AtomicU64; PRE_GAIN_BINS],
    /// frames whose pre-gain peak would clip at x4 (`peak * 4 > 32767`)
    would_clip_frames: AtomicU64,
    /// samples that left the server within one step of +-full scale (both signs), after the gain
    saturated_samples: AtomicU64,
    queued_to_app: AtomicU64,
    dropped_to_app: AtomicU64,
    received_from_app: AtomicU64,
    dropped_from_app: AtomicU64,
    /// the station-to-app gain stage (`DOWNLINK_GAIN`) and the app-to-station one (`UplinkAgc`)
    down: GainMeter,
    up: GainMeter,
}

impl Default for CallCounters {
    fn default() -> Self {
        CallCounters {
            enabled: AtomicBool::new(false),
            station_frames: AtomicU64::new(0),
            hist: std::array::from_fn(|_| AtomicU64::new(0)),
            would_clip_frames: AtomicU64::new(0),
            saturated_samples: AtomicU64::new(0),
            queued_to_app: AtomicU64::new(0),
            dropped_to_app: AtomicU64::new(0),
            received_from_app: AtomicU64::new(0),
            dropped_from_app: AtomicU64::new(0),
            down: GainMeter::new(),
            up: GainMeter::new(),
        }
    }
}

/// The cumulative values the last windowed line was taken at.
#[derive(Clone, Copy, Debug, Default)]
pub struct WindowPrev {
    counts: [u64; 6],
    down: MeterSnap,
    up: MeterSnap,
}

impl CallCounters {
    pub fn enable(&self) {
        self.enabled.store(true, Ordering::Relaxed);
    }

    pub fn enabled(&self) -> bool {
        self.enabled.load(Ordering::Relaxed)
    }

    /// One app microphone frame at the link rate, before the AGC (`pre`) and as it leaves
    /// (`post`).
    pub fn uplink_frame(&self, pre: &[u8], post: &[u8], step: AgcStep) {
        if self.enabled() {
            self.up.note(step.gain, step.held, pre, post, step.limited);
        }
    }

    /// One station frame at the link rate, before the gain (`pre`) and as it leaves (`post`);
    /// `limited` samples were bent by the limiter.
    pub fn downlink_frame(&self, pre: &[u8], post: &[u8], limited: usize) {
        if !self.enabled() {
            return;
        }
        self.down.note(DOWNLINK_GAIN as f32, false, pre, post, limited);
        let peak = pre
            .as_chunks::<2>()
            .0
            .iter()
            .map(|p| i32::from(i16::from_le_bytes([p[0], p[1]])).unsigned_abs())
            .max()
            .unwrap_or(0);
        self.station_frames.fetch_add(1, Ordering::Relaxed);
        self.hist[pre_gain_bin(peak)].fetch_add(1, Ordering::Relaxed);
        if peak.saturating_mul(DOWNLINK_GAIN as u32) > 32767 {
            self.would_clip_frames.fetch_add(1, Ordering::Relaxed);
        }
        let sat = post
            .as_chunks::<2>()
            .0
            .iter()
            .filter(|p| {
                let v = i16::from_le_bytes([p[0], p[1]]);
                v.unsigned_abs() >= 32766 // within one step of full scale (the limiter tends to it)
            })
            .count();
        if sat > 0 {
            self.saturated_samples
                .fetch_add(sat as u64, Ordering::Relaxed);
        }
    }

    fn count(&self, c: &AtomicU64) {
        if self.enabled() {
            c.fetch_add(1, Ordering::Relaxed);
        }
    }

    /// The numbers as the `call_audio_stats` fields (`rate` is the call's, 8000 or 16000).
    pub fn snapshot(&self, rate: u32) -> Value {
        let g = |c: &AtomicU64| c.load(Ordering::Relaxed);
        let hist: Vec<u64> = self.hist.iter().map(g).collect();
        let mut v = json!({
            "station_rate": rate,
            "station_frames": g(&self.station_frames),
            "pre_gain_hist": hist,
            "would_clip_frames": g(&self.would_clip_frames),
            "saturated_samples": g(&self.saturated_samples),
            "queued_to_app": g(&self.queued_to_app),
            "dropped_to_app": g(&self.dropped_to_app),
            "received_from_app": g(&self.received_from_app),
            "dropped_from_app": g(&self.dropped_from_app),
        });
        // scalars for the INFO line: the level of the station's frames before the gain (the
        // lowest, median and 95th-percentile frame peak, as bin upper bounds) and what each gain
        // stage did
        let m = v.as_object_mut().expect("an object");
        let lowest = hist.iter().position(|n| *n > 0).map(|i| -90 + 3 * i as i64);
        for (k, lvl) in [
            ("pre_gain_min_dbfs", lowest),
            ("pre_gain_p50_dbfs", hist_level_dbfs(&hist, 0.5)),
            ("pre_gain_p95_dbfs", hist_level_dbfs(&hist, 0.95)),
        ] {
            if let Some(l) = lvl {
                m.insert(k.to_string(), json!(l));
            }
        }
        self.down.fields("down_", m);
        self.up.fields("up_", m);
        v
    }

    /// The windowed line for the call's audio (D-20261121-ee09): counter deltas since `prev`
    /// plus both gain stages' window levels and the queue depths now; `None` while counting is
    /// off. Starts the next window.
    pub fn window_line(&self, prev: &mut WindowPrev, down_depth: usize, up_depth: usize) -> Option<String> {
        if !self.enabled() {
            return None;
        }
        let g = |c: &AtomicU64| c.load(Ordering::Relaxed);
        let cur = [
            g(&self.station_frames),
            g(&self.queued_to_app),
            g(&self.dropped_to_app),
            g(&self.received_from_app),
            g(&self.dropped_from_app),
            g(&self.saturated_samples),
        ];
        let d: Vec<u64> = cur.iter().zip(prev.counts).map(|(a, b)| a.saturating_sub(b)).collect();
        // does the quiet uplink go with a loud downlink (acoustic echo into the tablet's mic)? the
        // window's downlink level as sent, the uplink level as received, their difference, and
        // how much of the downlink sat at full scale
        let (dn, up) = (self.down.snap(), self.up.snap());
        let dsamp = dn.samples.saturating_sub(prev.down.samples);
        let down_rms = rms_dbfs(dn.sumsq_post.saturating_sub(prev.down.sumsq_post), dsamp);
        let up_rms = rms_dbfs(
            up.sumsq_pre.saturating_sub(prev.up.sumsq_pre),
            up.samples.saturating_sub(prev.up.samples),
        );
        let sat_ratio = if dsamp == 0 { 0.0 } else { (d[5] as f64 / dsamp as f64 * 1e4).round() / 1e4 };
        let line = format!(
            "audio window: station_frames=+{} queued_to_app=+{} dropped_to_app=+{} received_from_app=+{} dropped_from_app=+{} queue_to_app={}/{} queue_from_app={}/{} | down {} | up {} | echo_proxy down_rms_dbfs={} up_rms_dbfs={} up_minus_down_db={:.1} down_saturated_ratio={}",
            d[0], d[1], d[2], d[3], d[4], down_depth, CLIENT_QUEUE, up_depth, CLIENT_QUEUE,
            self.down.window(&prev.down),
            self.up.window(&prev.up),
            down_rms,
            up_rms,
            up_rms - down_rms,
            sat_ratio,
        );
        *prev = WindowPrev { counts: cur, down: self.down.snap(), up: self.up.snap() };
        Some(line)
    }
}

/// What the server does for an [`AudioClient`] (Go reaches into `c.s`).
pub trait ClientHost: Send + Sync {
    /// `AudioClient.release`: the client leaves. Go: under the server lock, if the server's
    /// current client is not this one, nothing happens (so a second call is a no-op); else
    /// the server forgets it. `deliberate` (`goodbye`): close it with "the client said
    /// goodbye" and hang the call up at once (BYE) if it is still the live call. Otherwise
    /// (`detach`): close it with "the client detached" and, if the call is live and answered,
    /// start the orphan-grace timer that hangs up when nobody re-attaches. Note that
    /// `CallAudio::detach` calls `goodbye` (audio.go `Detach`), not `AudioClient::detach`.
    /// The host calls `client.close(..)` for the one that is current.
    fn release(&self, client: &AudioClient, deliberate: bool);

    /// DEV-811 P3: the client's audio is over (its frames channel closed) and its counters were
    /// on: `stats` is the `call_audio_stats` payload. Default: nothing (a host that does not
    /// report).
    fn call_stats(&self, _call_id: &str, _stats: Value) {}
}

struct ClientState {
    closed: bool,
    reason: String,
    /// `None` once closed: dropping it is `close(c.to)`.
    to_tx: Option<mpsc::Sender<Vec<u8>>>,
}

/// The one client that owns an answered call's audio (`AudioClient`).
pub struct AudioClient {
    /// Read off the call, never assumed: 8000/320 on an A-law call, 16000/640 on a wideband
    /// one. (Frames from the station can be bigger than `frame_bytes`: its ptime is 40 ms, so a
    /// wideband payload is 1280 bytes. `frame_bytes` describes what the client should SEND.)
    pub rate: u32,
    pub frame_bytes: usize,

    host: Weak<dyn ClientHost>,
    stats: Arc<AudioStats>,
    state: Mutex<ClientState>,
    to_rx: Mutex<Option<mpsc::Receiver<Vec<u8>>>>,
    from_tx: mpsc::Sender<Vec<u8>>,
    from_rx: Mutex<Option<mpsc::Receiver<Vec<u8>>>>,
    done: CancelToken,
    /// DEV-811 P3: this call's own counters (off until enabled)
    counters: CallCounters,
    /// a client leg took the pair (`callAudio.Attach`); an outbound call's "nobody joined"
    /// timer reads this (dial.go)
    leg: AtomicBool,
}

impl AudioClient {
    pub fn new(
        rate: u32,
        frame_bytes: usize,
        host: Weak<dyn ClientHost>,
        stats: Arc<AudioStats>,
    ) -> Arc<AudioClient> {
        let (to_tx, to_rx) = mpsc::channel(CLIENT_QUEUE);
        let (from_tx, from_rx) = mpsc::channel(CLIENT_QUEUE);
        Arc::new(AudioClient {
            rate,
            frame_bytes,
            host,
            stats,
            state: Mutex::new(ClientState {
                closed: false,
                reason: String::new(),
                to_tx: Some(to_tx),
            }),
            to_rx: Mutex::new(Some(to_rx)),
            from_tx,
            from_rx: Mutex::new(Some(from_rx)),
            done: CancelToken::new(),
            counters: CallCounters::default(),
            leg: AtomicBool::new(false),
        })
    }

    /// DEV-811 P3: start counting this call's audio (the server calls it when `call_stats` is on).
    pub fn enable_stats(&self) {
        self.counters.enable();
    }

    pub fn counters(&self) -> &CallCounters {
        &self.counters
    }

    /// At the end of the client's audio: hands the counters to the server, if they were on.
    fn report_stats(&self, call_id: &str) {
        if !self.counters.enabled() {
            return;
        }
        if let Some(h) = self.host.upgrade() {
            h.call_stats(call_id, self.counters.snapshot(self.rate));
        }
    }

    /// `Frames()`: the station's audio, closed when the call is over. Taken once (by the
    /// downlink); `None` afterwards.
    pub fn take_frames(&self) -> Option<mpsc::Receiver<Vec<u8>>> {
        lock(&self.to_rx).take()
    }

    /// The microphone queue (`from`), read by the server's RTP sender. Taken once.
    pub fn take_microphone(&self) -> Option<mpsc::Receiver<Vec<u8>>> {
        lock(&self.from_rx).take()
    }

    /// Frames waiting now in the app-bound and station-bound queues (of `CLIENT_QUEUE` each).
    pub fn queue_depths(&self) -> (usize, usize) {
        let down = lock(&self.state)
            .to_tx
            .as_ref()
            .map_or(0, |t| t.max_capacity() - t.capacity());
        (down, self.from_tx.max_capacity() - self.from_tx.capacity())
    }

    /// `Done()`: cancelled when the call is over or this client was released.
    pub fn done(&self) -> CancelToken {
        self.done.clone()
    }

    /// `Reason()`: why the client was dropped; empty while it is attached.
    pub fn reason(&self) -> String {
        lock(&self.state).reason.clone()
    }

    pub fn is_closed(&self) -> bool {
        lock(&self.state).closed
    }

    /// `Send`: queues one microphone frame (PCM16 LE). Late microphone audio is worse than
    /// none, so a full queue drops the frame (and counts it); only a finished call is an error.
    pub fn send(&self, pcm: &[u8]) -> Result<(), CallOver> {
        let st = lock(&self.state);
        if st.closed {
            return Err(CallOver);
        }
        self.stats.frames_from_app.fetch_add(1, Ordering::SeqCst);
        self.counters.count(&self.counters.received_from_app);
        if self.from_tx.try_send(pcm.to_vec()).is_err() {
            self.stats.dropped_up.fetch_add(1, Ordering::SeqCst);
            self.counters.count(&self.counters.dropped_from_app);
        }
        Ok(())
    }

    /// The RTP read path's hand-off of a station frame (the `select { case cl.to <- pcm: ...
    /// default: ... }` in `rtpLoop`): never blocks, a full queue drops the frame and counts it
    /// per direction. `false` once the call is over (the server would have no current client).
    pub fn push_down(&self, pcm: Vec<u8>) -> bool {
        let st = lock(&self.state);
        let Some(tx) = &st.to_tx else { return false };
        if tx.try_send(pcm).is_ok() {
            self.stats.frames_to_app.fetch_add(1, Ordering::SeqCst);
            self.counters.count(&self.counters.queued_to_app);
        } else {
            self.stats.dropped_down.fetch_add(1, Ordering::SeqCst);
            self.counters.count(&self.counters.dropped_to_app);
        }
        true
    }

    /// `closeLocked`: marks the client over, closes the frames channel and `done`. The first
    /// call wins; later ones change nothing (the reason stays).
    pub fn close(&self, why: &str) {
        let mut st = lock(&self.state);
        if st.closed {
            return;
        }
        st.closed = true;
        st.reason = why.to_string();
        st.to_tx = None;
        drop(st);
        self.done.cancel();
    }

    /// `Goodbye`: leaves ON PURPOSE; the call ends at once (BYE), without the orphan grace.
    pub fn goodbye(&self) {
        self.release(true);
    }

    /// `Detach`: leaves WITHOUT saying so (a vanished phone); the call is given the orphan
    /// grace, because from here a hang-up and a hiccup look the same.
    pub fn detach(&self) {
        self.release(false);
    }

    fn release(&self, deliberate: bool) {
        match self.host.upgrade() {
            Some(h) => h.release(self, deliberate),
            // the server is gone: nothing else will ever close this client
            None => self.close(if deliberate {
                "the client said goodbye"
            } else {
                "the client detached"
            }),
        }
    }

    pub fn leg(&self) -> bool {
        self.leg.load(Ordering::SeqCst)
    }

    pub fn set_leg(&self) {
        self.leg.store(true, Ordering::SeqCst);
    }
}

// ── the converters ────────────────────────────────────────────────────────

/// `applyGain`: a COPY of PCM16 LE `b` scaled by `gain` with hard saturation. Empty input, or a
/// gain of 1 or less, comes back unchanged (an odd trailing byte included).
pub fn apply_gain(b: &[u8], gain: i64) -> Vec<u8> {
    if b.is_empty() || gain <= 1 {
        return b.to_vec();
    }
    b.as_chunks::<2>()
        .0
        .iter()
        .flat_map(|p| {
            let v = (i64::from(i16::from_le_bytes([p[0], p[1]])) * gain).clamp(-32768, 32767);
            (v as i16).to_le_bytes()
        })
        .collect()
}

/// [`apply_gain`] with a soft limiter instead of the hard clamp: `gain x sample` exactly while
/// its magnitude is at most [`LIMITER_KNEE`], above that the excess is squeezed by a tanh curve
/// that is continuous with a continuous slope at the knee and tends to full scale, so a loud
/// peak is rounded, not flattened (a clamp adds odd harmonics across the whole band; the
/// rounded peak adds far less). Stateless per sample, so no attack/release artefacts and no
/// state across frames. Returns the samples and how many were limited. Gain of 1 or less and
/// empty input come back unchanged, as in `apply_gain`.
pub fn apply_gain_soft(b: &[u8], gain: i64) -> (Vec<u8>, usize) {
    if b.is_empty() || gain <= 1 {
        return (b.to_vec(), 0);
    }
    let mut limited = 0;
    let out = b
        .as_chunks::<2>()
        .0
        .iter()
        .flat_map(|p| {
            let (y, l) = soft_limit(f64::from(i16::from_le_bytes([p[0], p[1]])) * gain as f64);
            limited += usize::from(l);
            y.to_le_bytes()
        })
        .collect();
    (out, limited)
}

/// One sample of the soft limiter: `v` (already scaled) unchanged up to [`LIMITER_KNEE`], above it
/// squeezed by the tanh curve; and whether it was squeezed.
fn soft_limit(v: f64) -> (i16, bool) {
    let a = v.abs();
    if a <= LIMITER_KNEE as f64 {
        return (v.round().clamp(-32768.0, 32767.0) as i16, false);
    }
    let room = (32767 - LIMITER_KNEE) as f64;
    let m = (LIMITER_KNEE as f64 + room * ((a - LIMITER_KNEE as f64) / room).tanh()).round();
    ((if v < 0.0 { -m } else { m }).clamp(-32768.0, 32767.0) as i16, true)
}

/// The bounded automatic gain on the app-to-station uplink (see the `UPLINK_AGC_*` constants).
/// One per call, owned by the uplink task. Per frame: measure its peak and RMS; if the RMS is
/// above the gate, the wanted gain is `TARGET_PEAK / peak` clamped to `[1, MAX]` and the gain
/// moves toward it (fast down, slow up); below the gate it stays where it is. The frame is
/// scaled with the gain ramping linearly from the previous frame's value (no steps), through the
/// soft limiter, so it cannot clip.
#[derive(Debug)]
pub struct UplinkAgc {
    /// the tracked gain (slow attack, slower release)
    gain: f32,
    /// the gain the last frame ended at: the tracked gain, or less when a loud frame needed the
    /// peak kept under the limiter's knee
    applied: f32,
}

/// What one frame through the AGC did.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct AgcStep {
    /// the gain at the end of the frame
    pub gain: f32,
    /// samples the limiter bent
    pub limited: usize,
    /// the frame was at or under the noise gate: the gain was held
    pub held: bool,
}

impl Default for UplinkAgc {
    fn default() -> Self {
        UplinkAgc { gain: 1.0, applied: 1.0 }
    }
}

impl UplinkAgc {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn gain(&self) -> f32 {
        self.gain
    }

    /// `frame` (PCM16 LE, 16 kHz) with the gain applied.
    pub fn process(&mut self, frame: &[u8]) -> (Vec<u8>, AgcStep) {
        let x: Vec<i16> = frame
            .as_chunks::<2>()
            .0
            .iter()
            .map(|p| i16::from_le_bytes([p[0], p[1]]))
            .collect();
        let n = x.len();
        let max = UPLINK_AGC_MAX_GAIN.max(1.0);
        if n == 0 || max <= 1.0 {
            self.gain = 1.0;
            self.applied = 1.0;
            return (frame.to_vec(), AgcStep { gain: 1.0, limited: 0, held: false });
        }
        let peak = x.iter().map(|v| f32::from(*v).abs()).fold(0.0, f32::max);
        let rms = (x.iter().map(|v| f32::from(*v).powi(2)).sum::<f32>() / n as f32).sqrt();
        let held = rms < UPLINK_AGC_GATE_RMS;
        if !held {
            let want = (UPLINK_AGC_TARGET_PEAK / peak.max(1.0)).clamp(1.0, max);
            let tc = if want < self.gain { UPLINK_AGC_ATTACK_S } else { UPLINK_AGC_RELEASE_S };
            let dur = n as f32 / LINK_RATE as f32;
            self.gain = (self.gain + (want - self.gain) * (1.0 - (-dur / tc).exp())).clamp(1.0, max);
        }
        // a frame whose own peak would pass the knee at the tracked gain is held under it (never
        // below 1): a shout after a quiet passage is not left to the slow attack
        let mut g0 = self.applied;
        let g1 = self.gain.min((LIMITER_KNEE as f32 / peak.max(1.0)).max(1.0));
        if g1 < g0 {
            g0 = g1; // going down is immediate for the whole frame; only the rise is ramped
        }
        self.applied = g1;
        if g0 == 1.0 && g1 == 1.0 {
            return (frame.to_vec(), AgcStep { gain: 1.0, limited: 0, held });
        }
        let mut limited = 0;
        let mut out = Vec::with_capacity(frame.len());
        for (i, v) in x.iter().enumerate() {
            let g = g0 + (g1 - g0) * ((i + 1) as f32 / n as f32);
            let (y, l) = soft_limit(f64::from(*v) * f64::from(g));
            limited += usize::from(l);
            out.extend_from_slice(&y.to_le_bytes());
        }
        (out, AgcStep { gain: g1, limited, held })
    }
}

// The rate and the gain stay `i64` on purpose (R1-175, declined): the observer's claim path
// (the observer's `claim.rs`) and the golden replays call these with `i64`, and both belong to
// other groups' files.

/// `toLinkRate`: PCM16 LE at `rate` to 16 kHz: pass-through at 16 kHz, sample doubling at
/// 8 kHz, `None` for a rate this client does not carry. A trailing odd byte is dropped.
pub fn to_link_rate(b: &[u8], rate: i64) -> Option<Vec<u8>> {
    let b = &b[..b.len() & !1];
    match rate {
        r if r == i64::from(LINK_RATE) => Some(b.to_vec()),
        8000 => {
            let mut out = Vec::with_capacity(b.len() * 2);
            for p in b.as_chunks::<2>().0 {
                out.extend_from_slice(&[p[0], p[1], p[0], p[1]]);
            }
            Some(out)
        }
        _ => None,
    }
}

/// `fromLinkRate`: 16 kHz PCM16 LE to the call's rate: pass-through at 16 kHz, pair-averaging
/// at 8 kHz (a trailing unpaired sample is dropped), `None` for a rate this client does not
/// carry.
pub fn from_link_rate(b: &[u8], rate: i64) -> Option<Vec<u8>> {
    let b = &b[..b.len() & !1];
    match rate {
        r if r == i64::from(LINK_RATE) => Some(b.to_vec()),
        8000 => {
            let mut out = Vec::with_capacity(b.len() / 2);
            for q in b.as_chunks::<4>().0 {
                let x = i32::from(i16::from_le_bytes([q[0], q[1]]));
                let y = i32::from(i16::from_le_bytes([q[2], q[3]]));
                out.extend_from_slice(&(((x + y) / 2) as i16).to_le_bytes());
            }
            Some(out)
        }
        _ => None,
    }
}

// ── the CallAudio ─────────────────────────────────────────────────────────

/// `callAudio`: the server's [`AudioClient`] as a [`crate::driver::CallAudio`].
pub struct CallAudioImpl {
    call: String,
    cl: Arc<AudioClient>,
    input: mpsc::Sender<Vec<u8>>,
    output: Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>>,
    attached: AtomicBool,
}

impl CallAudioImpl {
    /// `newCallAudio`: starts the two tasks (needs a tokio runtime). Panics if the client's
    /// frames were already taken: one client carries one pair.
    pub fn new(call_id: &str, cl: Arc<AudioClient>) -> Arc<CallAudioImpl> {
        let (in_tx, in_rx) = mpsc::channel(LINK_QUEUE);
        let (out_tx, out_rx) = mpsc::channel(LINK_QUEUE);
        let frames = cl
            .take_frames()
            .expect("the client's frames were already taken");
        tokio::spawn(downlink(call_id.to_string(), cl.clone(), frames, out_tx));
        tokio::spawn(uplink(cl.clone(), in_rx));
        tokio::spawn(report_windows(call_id.to_string(), cl.clone()));
        Arc::new(CallAudioImpl {
            call: call_id.to_string(),
            cl,
            input: in_tx,
            output: Arc::new(tokio::sync::Mutex::new(out_rx)),
            attached: AtomicBool::new(false),
        })
    }

    pub fn call_id(&self) -> &str {
        &self.call
    }

    pub fn client(&self) -> &Arc<AudioClient> {
        &self.cl
    }

    /// `over()`: the call is finished.
    pub fn over(&self) -> bool {
        self.cl.done().is_cancelled()
    }

    /// Whether a client leg has taken the pair.
    pub fn attached(&self) -> bool {
        self.attached.load(Ordering::SeqCst)
    }

    /// `a.attached.Store(true)`: the caller has it, `ClaimedAudio` will not hand it out again.
    pub fn mark_attached(&self) {
        self.attached.store(true, Ordering::SeqCst);
    }

    /// Test hook: the state of a dialled call nobody has joined yet (`attached.Store(false)`).
    #[cfg(feature = "test-seams")]
    pub fn clear_attached(&self) {
        self.attached.store(false, Ordering::SeqCst);
    }
}

impl crate::driver::CallAudio for CallAudioImpl {
    fn input(&self) -> mpsc::Sender<Vec<u8>> {
        self.input.clone()
    }

    fn output(&self) -> Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>> {
        self.output.clone()
    }

    /// Says a client leg has taken over the pair.
    fn attach(&self) {
        self.attached.store(true, Ordering::SeqCst);
        self.cl.set_leg();
    }

    /// Says the client leg is gone: the call is hung up at once (BYE), not after the orphan
    /// grace. Idempotent.
    fn detach(&self) {
        self.cl.goodbye();
    }
}

/// `downlink`: the station's frames (any even length, at the call's rate) become 1280-byte
/// 16 kHz frames with the x4 gain. A full queue drops the new frame: late audio is worse than
/// none. `out` is closed (its sender dropped) when the call is over, which ends the hub's copy.
async fn downlink(
    call_id: String,
    cl: Arc<AudioClient>,
    mut frames: mpsc::Receiver<Vec<u8>>,
    out: mpsc::Sender<Vec<u8>>,
) {
    let mut down: Vec<u8> = Vec::new();
    while let Some(frame) = frames.recv().await {
        let link = to_link_rate(&frame, i64::from(cl.rate)).unwrap_or_default();
        let (conv, limited) = if legacy_audio() {
            (apply_gain(&link, DOWNLINK_GAIN), 0)
        } else {
            apply_gain_soft(&link, DOWNLINK_GAIN)
        };
        cl.counters.downlink_frame(&link, &conv, limited);
        down.extend_from_slice(&conv);
        while down.len() >= LINK_FRAME_BYTES {
            let fr: Vec<u8> = down.drain(..LINK_FRAME_BYTES).collect();
            let _ = out.try_send(fr);
        }
    }
    // The frames channel is closed when the call (or this client) is over: the counters are final.
    cl.report_stats(&call_id);
}

/// How often the call's windowed audio line is logged (D-20261121-ee09).
pub const REPORT_EVERY: std::time::Duration = std::time::Duration::from_secs(5);

/// Logs [`CallCounters::window_line`] every `REPORT_EVERY` while the call's counting is on, until
/// the call is over. One bounded line per window (a fixed set of fields).
async fn report_windows(call_id: String, cl: Arc<AudioClient>) {
    let done = cl.done();
    let mut every = tokio::time::interval_at(tokio::time::Instant::now() + REPORT_EVERY, REPORT_EVERY);
    every.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut prev = WindowPrev::default();
    loop {
        tokio::select! {
            _ = done.cancelled() => return,
            _ = every.tick() => {
                let (down, up) = cl.queue_depths();
                if let Some(l) = cl.counters.window_line(&mut prev, down, up) {
                    crate::panic_hook::out_line(&format!("portico-transport: gate_sip_server call={call_id} {l}"));
                }
            }
        }
    }
}

/// `uplink`: the browser's frames (any even length, 16 kHz) become the call's rate and are
/// sent in `frame_bytes` pieces (what the RTP sender takes one of per 20 ms). `send` drops when
/// the server's queue is full. Ends when the call does.
async fn uplink(cl: Arc<AudioClient>, mut input: mpsc::Receiver<Vec<u8>>) {
    let done = cl.done();
    let mut mic: Vec<u8> = Vec::new();
    let mut agc = UplinkAgc::new();
    loop {
        tokio::select! {
            biased;
            _ = done.cancelled() => return,
            b = input.recv() => {
                let Some(raw) = b else { return };
                // the app's microphone raised for the station's speaker; the WebRTC leg's
                // decoded uplink and the frames leg's both arrive here
                let (b, step) = if legacy_audio() {
                    (raw.clone(), AgcStep { gain: 1.0, limited: 0, held: false })
                } else {
                    agc.process(&raw)
                };
                cl.counters.uplink_frame(&raw, &b, step);
                mic.extend_from_slice(&from_link_rate(&b, i64::from(cl.rate)).unwrap_or_default());
                let fb = cl.frame_bytes;
                while fb > 0 && mic.len() >= fb {
                    let _ = cl.send(&mic[..fb]);
                    mic.drain(..fb);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::driver::CallAudio;

    /// A stand-in for the part of the Go `Server` that `AudioClient.release` touches: the one
    /// current client, and a record of the hang-ups it asked for.
    struct FakeHost {
        current: Mutex<Option<Arc<AudioClient>>>,
        hangups: Mutex<Vec<String>>,
        orphan_timers: Mutex<u32>,
        reported: Mutex<Vec<(String, Value)>>,
    }

    impl FakeHost {
        fn new() -> Arc<FakeHost> {
            Arc::new(FakeHost {
                current: Mutex::new(None),
                hangups: Mutex::new(Vec::new()),
                orphan_timers: Mutex::new(0),
                reported: Mutex::new(Vec::new()),
            })
        }
        fn client(self: &Arc<Self>, rate: u32, fb: usize) -> (Arc<AudioClient>, Arc<AudioStats>) {
            let stats = Arc::new(AudioStats::default());
            let host: Arc<dyn ClientHost> = self.clone();
            let cl = AudioClient::new(rate, fb, Arc::downgrade(&host), stats.clone());
            *self.current.lock().unwrap() = Some(cl.clone());
            (cl, stats)
        }
        /// `endLocked`: the call ended; forget the client and close it.
        fn end_call(&self, why: &str) {
            if let Some(cl) = self.current.lock().unwrap().take() {
                cl.close(why);
            }
        }
        fn attached(&self) -> bool {
            self.current.lock().unwrap().is_some()
        }
    }

    impl ClientHost for FakeHost {
        fn call_stats(&self, call_id: &str, stats: Value) {
            self.reported.lock().unwrap().push((call_id.to_string(), stats));
        }
        fn release(&self, client: &AudioClient, deliberate: bool) {
            let mut cur = self.current.lock().unwrap();
            match &*cur {
                Some(c) if std::ptr::eq(Arc::as_ptr(c), client) => {}
                _ => return,
            }
            *cur = None;
            if deliberate {
                client.close("the client said goodbye");
                self.hangups
                    .lock()
                    .unwrap()
                    .push("the client hung up".into());
                return;
            }
            client.close("the client detached");
            *self.orphan_timers.lock().unwrap() += 1;
        }
    }

    async fn settle() {
        for _ in 0..40 {
            tokio::task::yield_now().await;
        }
    }

    // TestParity_BackpressureDropsAndCounts (parity_test.go), the audio half: the server half
    // (40 RTP packets in over a socket, `Snapshot().RTPIn`) is in `tests/server_parity.rs`.
    #[tokio::test]
    async fn backpressure_drops_and_counts_per_direction() {
        let host = FakeHost::new();
        let (cl, stats) = host.client(8000, 320);
        for _ in 0..40 {
            // never read cl.take_frames()
            assert!(cl.push_down(vec![0; 320]));
        }
        let (to_app, _, _, dropped_down) = stats.snapshot();
        assert_eq!((to_app, dropped_down), (6, 34), "queue of 6: 40 packets");
        for _ in 0..50 {
            cl.send(&[0; 320]).unwrap();
        }
        let (_, from_app, dropped_up, _) = stats.snapshot();
        assert_eq!(from_app, 50);
        assert!(dropped_up > 0, "dropped up {dropped_up}");
        cl.detach();
    }

    // TestParity_SecondAttachRefusedNotDisplaced (parity_test.go) depends on the server; this
    // is the part this file owns: a closed client refuses Send, and Close keeps the first reason.
    #[tokio::test]
    async fn closed_client_refuses_send_and_keeps_its_first_reason() {
        let host = FakeHost::new();
        let (cl, stats) = host.client(16000, 640);
        assert_eq!(cl.reason(), "");
        host.end_call("the call was hung up");
        assert!(cl.is_closed() && cl.done().is_cancelled());
        assert_eq!(cl.send(&[1, 2]), Err(CallOver));
        assert_eq!(CallOver.to_string(), "the call is over");
        assert!(!cl.push_down(vec![1]));
        cl.close("later");
        assert_eq!(cl.reason(), "the call was hung up");
        assert_eq!(stats.snapshot(), (0, 0, 0, 0));
        // frames channel is closed
        let mut rx = cl.take_frames().unwrap();
        assert!(rx.recv().await.is_none());
        assert!(cl.take_frames().is_none());
    }

    #[tokio::test]
    async fn goodbye_asks_the_host_to_hang_up_and_is_idempotent() {
        let host = FakeHost::new();
        let (cl, _) = host.client(16000, 640);
        cl.goodbye();
        cl.goodbye();
        cl.detach();
        assert_eq!(host.hangups.lock().unwrap().len(), 1);
        assert_eq!(*host.orphan_timers.lock().unwrap(), 0);
        assert!(!host.attached());
        assert_eq!(cl.reason(), "the client said goodbye");
    }

    #[tokio::test]
    async fn detach_gives_the_orphan_grace_and_keeps_the_call() {
        let host = FakeHost::new();
        let (cl, _) = host.client(16000, 640);
        cl.detach();
        assert_eq!(*host.orphan_timers.lock().unwrap(), 1);
        assert!(host.hangups.lock().unwrap().is_empty());
        assert_eq!(cl.reason(), "the client detached");
    }

    #[tokio::test]
    async fn a_client_whose_server_is_gone_still_closes() {
        let host = FakeHost::new();
        let (cl, _) = host.client(16000, 640);
        drop(host);
        cl.goodbye();
        assert!(cl.is_closed());
    }

    // TestW3DetachHangsUpAtOnce (audio_test.go), the audio half: Detach is Goodbye, twice is
    // fine, Out closes, nothing after it. The 'BYE reaches the station' half is in `tests/server_*.rs`.
    #[tokio::test]
    async fn detach_hangs_up_at_once_and_closes_out() {
        let host = FakeHost::new();
        let (cl, _) = host.client(16000, 640);
        let a = CallAudioImpl::new("call-W3D", cl);
        a.attach();
        a.detach();
        a.detach();
        settle().await;
        assert_eq!(host.hangups.lock().unwrap().len(), 1);
        assert!(a.over());
        let out = a.output();
        assert!(
            out.lock().await.recv().await.is_none(),
            "Out not closed after hang-up"
        );
    }

    // TestW3StationByeClosesOut (audio_test.go), the audio half.
    #[tokio::test]
    async fn the_end_of_the_call_closes_out() {
        let host = FakeHost::new();
        let (cl, _) = host.client(16000, 640);
        let a = CallAudioImpl::new("call-W3E", cl);
        a.attach();
        host.end_call("the call was hung up");
        settle().await;
        assert!(a.output().lock().await.recv().await.is_none());
    }

    // the x4 gain saturates, the 8 kHz doubling, and a 1280 frame needs two 640 payloads
    #[tokio::test]
    async fn downlink_makes_1280_frames_with_gain() {
        let host = FakeHost::new();
        let (cl, _) = host.client(16000, 640);
        let a = CallAudioImpl::new("c", cl.clone());
        let pcm = [10000i16, -10000, 8191, 8192, -8192, -8193, 100, -100];
        let mut first: Vec<u8> = pcm.iter().flat_map(|v| v.to_le_bytes()).collect();
        first.resize(640, 0);
        assert!(cl.push_down(first));
        settle().await;
        assert!(
            a.output().lock().await.try_recv().is_err(),
            "half a frame is not enough"
        );
        assert!(cl.push_down(vec![0; 640]));
        settle().await;
        let f = a.output().lock().await.try_recv().expect("a frame");
        assert_eq!(f.len(), 1280);
        let got: Vec<i16> = f[..16]
            .chunks(2)
            .map(|p| i16::from_le_bytes([p[0], p[1]]))
            .collect();
        assert_eq!(
            got,
            // the soft limiter: 400 is linear (x4); the loud ones are rounded under full scale, not clamped to it
            [32323, -32323, 30676, 30677, -30677, -30679, 400, -400]
        );
    }

    #[test]
    fn converters_follow_go() {
        // applyGain: gain <= 1 and empty input come back untouched, odd byte included
        assert_eq!(apply_gain(&[1, 2, 3], 1), vec![1, 2, 3]);
        assert_eq!(apply_gain(&[1, 2, 3], 4), vec![4, 8]);
        assert_eq!(apply_gain(&[], 4), Vec::<u8>::new());
        // toLinkRate / fromLinkRate
        assert_eq!(to_link_rate(&[1, 2, 3], 16000), Some(vec![1, 2]));
        assert_eq!(to_link_rate(&[1, 2], 8000), Some(vec![1, 2, 1, 2]));
        assert_eq!(to_link_rate(&[1, 2], 44100), None);
        assert_eq!(from_link_rate(&[1, 0, 3, 0, 9], 8000), Some(vec![2, 0]));
        assert_eq!(from_link_rate(&[1, 0, 3, 0], 0), None);
        let m = (-1i16).to_le_bytes();
        let n = (-2i16).to_le_bytes();
        assert_eq!(
            from_link_rate(&[m[0], m[1], n[0], n[1]], 8000),
            Some((-1i16).to_le_bytes().to_vec()),
            "(-1 + -2) / 2 truncates toward zero"
        );
    }

    // R1-27: a panic while the client's lock is held (a contained handler panic) must not make
    // every later send/close/reason panic too.
    #[tokio::test]
    async fn a_poisoned_client_lock_does_not_spread() {
        let host = FakeHost::new();
        let (cl, _stats) = host.client(16000, 640);
        let c2 = cl.clone();
        let _ = std::thread::spawn(move || {
            let _g = c2.state.lock().unwrap();
            let _t = c2.to_rx.lock().unwrap();
            panic!("a handler panicked while holding the client's locks");
        })
        .join();
        assert!(cl.state.is_poisoned() && cl.to_rx.is_poisoned());
        assert_eq!(cl.reason(), "");
        assert!(!cl.is_closed());
        assert!(cl.send(&[0; 320]).is_ok());
        assert!(cl.take_frames().is_some());
        cl.close("done");
        assert_eq!(cl.reason(), "done");
    }

    // DEV-811 P3: the histogram's bins and their edges.
    #[test]
    fn pre_gain_bins_are_3_db_wide_from_minus_90() {
        assert_eq!(pre_gain_bin(0), 0, "digital silence");
        assert_eq!(pre_gain_bin(1), 0, "-90.3 dBFS is below the first bin");
        assert_eq!(pre_gain_bin(2), 2, "-84.3 dBFS, in [-87, -84)");
        assert_eq!(pre_gain_bin(32768), PRE_GAIN_BINS - 1, "full scale");
        assert_eq!(pre_gain_bin(32767), PRE_GAIN_BINS - 1);
        // 8192 = -12.04 dBFS: where x4 (+12.04 dB) first reaches full scale; [-15,-12) is bin 26
        assert_eq!(pre_gain_bin(8192), 26);
        assert_eq!(pre_gain_bin(8191), 26);
        // 16384 = -6.02 dBFS, [-9,-6) is bin 28
        assert_eq!(pre_gain_bin(16384), 28);
        // bin 30 starts at -3 dBFS = 23197.0 of 32768
        assert_eq!(pre_gain_bin(23170), 29);
        assert_eq!(pre_gain_bin(23196), 29);
        assert_eq!(pre_gain_bin(23198), 30);
    }

    fn pcm(samples: &[i16]) -> Vec<u8> {
        samples.iter().flat_map(|s| s.to_le_bytes()).collect()
    }

    fn gain_frame(c: &CallCounters, pre: &[u8]) {
        let (post, limited) = apply_gain_soft(pre, DOWNLINK_GAIN);
        c.downlink_frame(pre, &post, limited);
    }

    #[test]
    fn counters_count_pre_gain_peak_clip_frames_and_post_gain_saturation() {
        let c = CallCounters::default();
        c.enable();
        // a quiet frame: peak 1000, x4 stays under full scale
        let quiet = pcm(&[1000, -500, 200]);
        gain_frame(&c, &quiet);
        // a loud one: peak 9000 would clip at x4 and is limited (not saturated), the 3rd sample is linear
        let loud = pcm(&[9000, -9000, 100]);
        gain_frame(&c, &loud);
        // -32768 pre-gain is peak 32768 (not 0 from a wrapped abs)
        let neg = pcm(&[i16::MIN]);
        gain_frame(&c, &neg);
        let v = c.snapshot(16000);
        assert_eq!(v["station_frames"], 3);
        assert_eq!(v["would_clip_frames"], 2);
        // only the full-scale sample (-32768 x4) ends at full scale: 9000 x4 is rounded to ~31.7k
        assert_eq!(v["saturated_samples"], 1);
        assert_eq!(v["down_limited_frames"], 2);
        assert_eq!(v["down_limited_samples"], 3);
        assert_eq!(v["down_gain"], 4.0);
        let hist = v["pre_gain_hist"].as_array().unwrap();
        assert_eq!(hist.len(), PRE_GAIN_BINS);
        assert_eq!(hist.iter().map(|n| n.as_u64().unwrap()).sum::<u64>(), 3);
        assert_eq!(hist[pre_gain_bin(1000)], 1);
        assert_eq!(hist[pre_gain_bin(9000)], 1);
        assert_eq!(hist[PRE_GAIN_BINS - 1], 1);
        assert_eq!(v["station_rate"], 16000);
    }

    #[test]
    fn counters_are_inert_until_enabled() {
        let c = CallCounters::default();
        let loud = pcm(&[30000, -30000]);
        gain_frame(&c, &loud);
        c.count(&c.queued_to_app);
        let v = c.snapshot(8000);
        assert_eq!(v["station_frames"], 0);
        assert_eq!(v["saturated_samples"], 0);
        assert_eq!(v["queued_to_app"], 0);
        assert!(v["pre_gain_hist"].as_array().unwrap().iter().all(|n| n == 0));
    }

    // the call's end reaches the host once, with what the call carried, and only when counting was on
    #[tokio::test]
    async fn the_end_of_an_answered_call_reports_its_counters_once() {
        let host = FakeHost::new();
        let (cl, _) = host.client(8000, 320);
        cl.enable_stats();
        let a = CallAudioImpl::new("call-S1", cl.clone());
        a.attach();
        for _ in 0..3 {
            assert!(cl.push_down(vec![0x10; 320])); // 160 samples at 8 kHz
        }
        cl.send(&[0; 320]).unwrap();
        settle().await;
        assert!(host.reported.lock().unwrap().is_empty(), "not before the end");
        host.end_call("the call was hung up");
        settle().await;
        cl.close("again");
        settle().await;
        let got = host.reported.lock().unwrap().clone();
        assert_eq!(got.len(), 1, "{got:?}");
        assert_eq!(got[0].0, "call-S1");
        assert_eq!(got[0].1["station_rate"], 8000);
        assert_eq!(got[0].1["station_frames"], 3);
        assert_eq!(got[0].1["queued_to_app"], 3);
        assert_eq!(got[0].1["received_from_app"], 1);
        assert_eq!(got[0].1["saturated_samples"], 0);
    }

    #[tokio::test]
    async fn with_counting_off_the_end_of_a_call_reports_nothing() {
        let host = FakeHost::new();
        let (cl, _) = host.client(16000, 640);
        let a = CallAudioImpl::new("call-S2", cl.clone());
        a.attach();
        assert!(cl.push_down(vec![0x10; 1280]));
        host.end_call("the call was hung up");
        settle().await;
        assert!(host.reported.lock().unwrap().is_empty());
    }

    // ---- the soft limiter and the uplink gain (D-20261121) ----

    fn samples(b: &[u8]) -> Vec<i16> {
        b.chunks(2).map(|c| i16::from_le_bytes([c[0], c[1]])).collect()
    }

    #[test]
    fn the_soft_limiter_is_exactly_the_gain_below_the_knee_and_never_hard_clips() {
        // below the knee (gain x |sample| <= 24000) it equals the hard gain, so the audible gain is kept
        let lin: Vec<i16> = (-6000..=6000).step_by(7).collect();
        let (soft, limited) = apply_gain_soft(&pcm(&lin), 4);
        assert_eq!(limited, 0);
        assert_eq!(soft, apply_gain(&pcm(&lin), 4));
        // above it: monotonic, symmetric, always under full scale, and not flat-topped
        let loud: Vec<i16> = (6001..=32767).step_by(13).map(|v| v as i16).collect();
        let (out, limited) = apply_gain_soft(&pcm(&loud), 4);
        assert_eq!(limited, loud.len());
        let o = samples(&out);
        assert!(o.windows(2).all(|w| w[1] >= w[0]), "monotonic");
        assert!(o.iter().all(|v| (24000..=32767).contains(&i32::from(*v))));
        let peak_in_4k = o[loud.iter().position(|v| *v >= 10000).unwrap()];
        assert!(peak_in_4k > 24000 && peak_in_4k < 32500, "9000-ish x4 is bent, not clamped: {peak_in_4k}");
        let distinct: std::collections::BTreeSet<_> = o.iter().take(o.len() / 2).collect();
        assert!(distinct.len() > 100, "a hard clamp would collapse these to one value");
        let (neg, _) = apply_gain_soft(&pcm(&[-10000, 10000]), 4);
        let n = samples(&neg);
        assert_eq!(n[0], -n[1], "symmetric");
        // the hard clamp this replaces: the same 10000 sample was flat at full scale
        assert_eq!(samples(&apply_gain(&pcm(&[10000]), 4))[0], 32767);
        // gain 1, empty input: unchanged
        assert_eq!(apply_gain_soft(&pcm(&[5, 6]), 1), (pcm(&[5, 6]), 0));
        assert_eq!(apply_gain_soft(&[], 4), (vec![], 0));
    }

    #[test]
    fn the_gain_constants_are_the_documented_ones() {
        assert_eq!((DOWNLINK_GAIN, LIMITER_KNEE), (4, 24000));
        assert_eq!(UPLINK_AGC_MAX_GAIN, 8.0);
        assert_eq!(UPLINK_AGC_TARGET_PEAK, 16384.0);
        assert!(UPLINK_AGC_ATTACK_S < UPLINK_AGC_RELEASE_S, "slow release");
    }

    /// 20 ms (320 samples) of a 300 Hz tone of amplitude `a`, frame `k` of a continuous tone.
    fn tone(a: f64, k: usize) -> Vec<u8> {
        let s: Vec<i16> = (0..320)
            .map(|i| (a * (((k * 320 + i) as f64) * 2.0 * std::f64::consts::PI * 300.0 / 16000.0).sin()) as i16)
            .collect();
        pcm(&s)
    }

    fn peak_of(b: &[u8]) -> i32 {
        samples(b).iter().map(|v| i32::from(*v).abs()).max().unwrap_or(0)
    }

    /// Runs `n` frames of amplitude `a` through `agc`; the last output frame and every gain seen.
    fn run(agc: &mut UplinkAgc, a: f64, n: usize) -> (Vec<u8>, Vec<f32>) {
        let (mut last, mut gains) = (vec![], vec![]);
        for k in 0..n {
            let (o, st) = agc.process(&tone(a, k));
            gains.push(st.gain);
            last = o;
        }
        (last, gains)
    }

    #[test]
    fn a_quiet_source_is_raised_toward_the_target_and_never_above_the_cap() {
        // amplitude 1500 (-27 dBFS) wants x10.9: capped at x8, reached slowly (3 s release), monotonic
        let mut agc = UplinkAgc::new();
        let (out, gains) = run(&mut agc, 1500.0, 750); // 15 s
        assert!(gains.iter().all(|g| *g >= 1.0 && *g <= UPLINK_AGC_MAX_GAIN), "{gains:?}");
        assert!(gains.windows(2).all(|w| w[1] >= w[0] - 1e-6), "no pumping down on a steady source");
        assert!(gains[0] < 1.05, "slow release: the first 20 ms barely move: {}", gains[0]);
        assert!(gains[149] < 7.0 && gains[749] > 7.9, "{} {}", gains[149], gains[749]);
        assert!(peak_of(&out) > 11_500 && peak_of(&out) < 12_500, "{}", peak_of(&out));
        // amplitude 4000 wants x4.1: settles at the target peak (-6 dBFS), not above
        let mut agc = UplinkAgc::new();
        let (out, _) = run(&mut agc, 4000.0, 750);
        let p = peak_of(&out);
        assert!((15_500..=16_500).contains(&p), "{p}");
    }

    #[test]
    fn a_loud_source_is_unchanged_byte_for_byte() {
        // a phone at -1 dBFS (and anything above the target): wanted gain <= 1, so gain 1
        for a in [18_000.0, 30_000.0] {
            let mut agc = UplinkAgc::new();
            for k in 0..100 {
                let f = tone(a, k);
                let (o, st) = agc.process(&f);
                assert_eq!(o, f, "amplitude {a}, frame {k}");
                assert_eq!((st.gain, st.limited), (1.0, 0));
            }
        }
    }

    #[test]
    fn silence_and_the_noise_floor_stay_as_they_are() {
        let mut agc = UplinkAgc::new();
        let z = pcm(&vec![0i16; 320]);
        let hiss = tone(15.0, 0); // rms 10, about -70 dBFS: under the gate
        for _ in 0..500 {
            let (o, st) = agc.process(&z);
            assert_eq!((o, st.gain, st.held), (z.clone(), 1.0, true));
            let (o, st) = agc.process(&hiss);
            assert_eq!((o, st.gain, st.held), (hiss.clone(), 1.0, true), "the gain is not raised on the noise floor");
        }
        // and after speech has raised the gain, a gated pause leaves it exactly where it was
        run(&mut agc, 1500.0, 250);
        let held_at = agc.gain();
        assert!(held_at > 2.0);
        for _ in 0..500 {
            let (_, st) = agc.process(&hiss);
            assert!(st.held);
        }
        assert_eq!(agc.gain(), held_at, "a 10 s pause below the gate does not move the gain");
    }

    #[test]
    fn the_gain_does_not_pump_on_a_pause_above_the_gate() {
        // speech at amplitude 3000 settles the gain; then 1 s of room noise at 300 (above the
        // gate, wants x8): release is 3 s, so the gain moves 28 % of the way, not to the cap
        let mut agc = UplinkAgc::new();
        run(&mut agc, 3000.0, 750);
        let before = agc.gain();
        run(&mut agc, 300.0, 50);
        let rise = agc.gain() - before;
        let gap = UPLINK_AGC_MAX_GAIN - before;
        assert!(rise > 0.0 && rise < 0.3 * gap, "rose {rise} of a {gap} gap");
    }

    #[test]
    fn a_shout_after_a_quiet_passage_does_not_clip() {
        let mut agc = UplinkAgc::new();
        run(&mut agc, 1500.0, 500); // gain near the cap
        assert!(agc.gain() > 7.0);
        let mut worst = 0;
        let mut flat = 0;
        for k in 0..25 {
            let (o, st) = agc.process(&tone(30_000.0, k));
            let sm = samples(&o);
            worst = worst.max(sm.iter().map(|v| i32::from(*v).abs()).max().unwrap());
            // a clipped waveform sits on the same extreme value for several samples
            flat += sm.windows(3).filter(|w| w[0] == w[1] && w[1] == w[2] && i32::from(w[0]).abs() > 30_000).count();
            if k >= 1 {
                assert!(st.gain <= 1.3, "the peak cap takes the gain down within a frame: {}", st.gain);
            }
        }
        assert!(worst <= 32_767);
        assert_eq!(flat, 0, "no flat tops");
    }

    // the app's microphone reaches the station through the AGC, from both legs
    #[tokio::test]
    async fn the_quiet_app_microphone_is_raised_through_the_agc_and_logged() {
        let host = FakeHost::new();
        let (cl, _) = host.client(16000, 640);
        cl.enable_stats();
        let a = CallAudioImpl::new("call-U1", cl.clone());
        let mut mic = cl.take_microphone().unwrap();
        let mut last = Vec::new();
        for k in 0..200 {
            // 20 ms of amplitude 1500 per frame
            a.input().send(tone(1500.0, k)).await.unwrap();
            last = tokio::time::timeout(std::time::Duration::from_secs(2), mic.recv()).await.unwrap().unwrap();
        }
        assert!(peak_of(&last) > 3000, "raised from 1500: {}", peak_of(&last));
        let v = cl.counters().snapshot(16000);
        assert_eq!(v["up_frames"], 200);
        assert!(v["up_gain_max"].as_f64().unwrap() > 2.0 && v["up_gain_min"].as_f64().unwrap() < 1.1);
        assert!(v["up_gain_avg"].as_f64().unwrap() > 1.0);
        assert!(v["up_rms_post_dbfs"].as_f64().unwrap() > v["up_rms_pre_dbfs"].as_f64().unwrap() + 3.0);
        cl.close("done");
    }

    #[test]
    fn the_snapshot_carries_the_level_summary_and_the_window_line_is_a_delta() {
        let c = CallCounters::default();
        c.enable();
        for peak in [200i16, 400, 400, 4000, 12000] {
            gain_frame(&c, &pcm(&vec![peak; 160]));
        }
        c.uplink_frame(&pcm(&vec![1000; 160]), &pcm(&vec![3000; 160]), AgcStep { gain: 3.0, limited: 0, held: false });
        let v = c.snapshot(16000);
        let min = v["pre_gain_min_dbfs"].as_i64().unwrap();
        let p50 = v["pre_gain_p50_dbfs"].as_i64().unwrap();
        let p95 = v["pre_gain_p95_dbfs"].as_i64().unwrap();
        assert!(min <= p50 && p50 <= p95, "{min} {p50} {p95}");
        assert!(p95 >= -12 && min < -40, "{min} {p95}");
        assert!(v["down_rms_pre_dbfs"].as_f64().unwrap() < 0.0);
        assert_eq!(v["up_peak_post_dbfs"].as_f64().unwrap(), -20.8);
        let mut prev = WindowPrev::default();
        let l1 = c.window_line(&mut prev, 2, 1).unwrap();
        assert!(l1.contains("station_frames=+5") && l1.contains("queue_to_app=2/6") && l1.contains("queue_from_app=1/6"), "{l1}");
        assert!(l1.contains("| down gain_min=4 gain_avg=4.00 gain_max=4 held_frames=+0 frames=+5 limited_frames=+1"), "{l1}");
        assert!(l1.contains("| up gain_min=3 gain_avg=3.00 gain_max=3 held_frames=+0 frames=+1"), "{l1}");
        assert!(l1.contains("| echo_proxy down_rms_dbfs=") && l1.contains("up_minus_down_db=") && l1.contains("down_saturated_ratio="), "{l1}");
        c.count(&c.dropped_from_app);
        gain_frame(&c, &pcm(&vec![300; 160]));
        let l2 = c.window_line(&mut prev, 0, 0).unwrap();
        assert!(l2.contains("station_frames=+1") && l2.contains("dropped_from_app=+1") && l2.contains("down gain_min=4 gain_avg=4.00 gain_max=4 held_frames=+0 frames=+1 limited_frames=+0"), "{l2}");
        assert!(l1.len() < 900 && l2.len() < 900, "{}", l1.len());
        assert!(CallCounters::default().window_line(&mut WindowPrev::default(), 0, 0).is_none(), "off: silent");
    }
}
