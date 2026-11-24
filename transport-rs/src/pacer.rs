//! `pkg/pacer`: the playout side of a real-time voice path (DEV-764 slice 7b). An adaptive jitter
//! buffer that turns bursty, unevenly timed audio frames into an even stream of one frame per
//! frame duration: the RFC 3550 jitter estimate, an adaptive playout delay, drop-oldest on
//! overflow, concealment by silence on underrun and slow drift correction. It is free of any
//! codec, network or transport dependency, so it is tested with a fake clock: every method that
//! takes a time takes a [`Duration`] since an arbitrary epoch (Go passes `time.Time`).
//!
//! Used by the front-door talk driver (`driver::unifiprotect::talk*`) in both directions: the
//! visitor's voice (go2rtc/ffmpeg, 40 ms frames) toward the phone, and the phone's microphone
//! toward the Protect talkback sink.
//!
//! The policy is Go's, byte for byte (`TestPacer*` ports and the golden vectors check it):
//!
//! * Priming: nothing is emitted until the first frames have been held for the target delay (or
//!   enough have queued to cover it).
//! * Even output: [`Pacer::tick`] emits exactly one frame per frame duration.
//! * Backlog cap, drop OLDEST: a burst is trimmed from the front down to `max`.
//! * Underrun: send SILENCE (only after the first real frame), then re-prime with a target one
//!   frame larger.
//! * Drift: if the depth stays beyond target + slack for `drift_ticks` consecutive ticks, one
//!   oldest frame is dropped.
//!
//! Differences from Go (PARITY.md 124 to 126): `stop` is not a wait on a goroutine but a flag
//! checked under the same lock as every `emit`, which gives the same guarantee (no `emit` runs
//! after `stop` returns); a panicking `emit` ends the loop instead of the process.

use crate::util::lock;
use std::collections::VecDeque;
use std::fmt;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::time::Instant;

/// Ticks between target adjustments.
const ADAPT_EVERY: u64 = 25;

/// `Config`. `frame_dur` is required.
#[derive(Clone, Debug, Default)]
pub struct Config {
    /// duration of one frame
    pub frame_dur: Duration,
    /// starting target playout delay (default 100 ms)
    pub initial: Duration,
    /// lowest target (default one frame)
    pub min: Duration,
    /// highest target AND the backlog cap (default 200 ms)
    pub max: Duration,
    /// frames above target tolerated before drift correction (default 2)
    pub slack: i64,
    /// how many consecutive ticks the excess must last before one frame is slipped (default 25)
    pub drift_ticks: i64,
    /// the frame emitted on an underrun after audio has started; `None` emits nothing
    pub silence: Option<Vec<u8>>,
}

/// `Stats`: the counters of one stream.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Stats {
    pub frames_in: u64,
    /// real frames emitted
    pub frames_out: u64,
    /// concealment frames emitted
    pub silence_out: u64,
    /// trimmed by the backlog cap
    pub drops_oldest: u64,
    /// slipped by drift correction
    pub drops_drift: u64,
    pub underruns: u64,
    /// largest interval between two arrivals
    pub max_gap: Duration,
    /// arrivals more than 100 ms after the previous one
    pub gaps_100: u64,
    /// RFC 3550 interarrival jitter estimate, ms
    pub jitter_ms: f64,
    /// frames queued now
    pub depth: i64,
    /// current target playout delay
    pub target: Duration,
}

impl fmt::Display for Stats {
    /// `Stats.String()`.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "in={} out={} silence={} dropOldest={} dropDrift={} underruns={} maxGap={}ms gaps>100ms={} jitter={:.1}ms depth={} target={}ms",
            self.frames_in,
            self.frames_out,
            self.silence_out,
            self.drops_oldest,
            self.drops_drift,
            self.underruns,
            self.max_gap.as_millis(),
            self.gaps_100,
            self.jitter_ms,
            self.depth,
            self.target.as_millis()
        )
    }
}

/// `Kind`: what a tick produced.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// nothing to send this tick
    None,
    /// a real frame
    Audio,
    /// a concealment frame
    Silence,
}

struct Inner {
    q: VecDeque<Vec<u8>>,
    /// arrival of the oldest queued frame (while priming), ns
    first_at: i64,
    last_at: i64,
    have_last: bool,
    primed: bool,
    /// a real frame has been emitted
    started: bool,
    target_fr: i64,
    drift_run: i64,
    ticks: u64,
    jitter: f64,
    st: Stats,
    runner_set: bool,
}

/// Safe for concurrent `push` and `tick` / `start`.
pub struct Pacer {
    cfg: Config,
    min_fr: i64,
    max_fr: i64,
    inner: Mutex<Inner>,
    /// the clock's zero: `now` is the time since it, the clock `start` ticks on
    epoch: Instant,
    /// taken around every `emit` and by `stop`: after `stop` returns no `emit` runs
    run: Arc<Mutex<bool>>,
    wake: Arc<tokio::sync::Notify>,
}

fn ns(d: Duration) -> i64 {
    i64::try_from(d.as_nanos()).unwrap_or(i64::MAX)
}

impl Pacer {
    /// `New`. Panics on a non-positive `frame_dur`, as Go does (a programming error: every
    /// caller in transport-rs passes a constant or a clamped value).
    pub fn new(mut cfg: Config) -> Pacer {
        assert!(!cfg.frame_dur.is_zero(), "pacer: FrameDur must be positive");
        if cfg.initial.is_zero() {
            cfg.initial = Duration::from_millis(100);
        }
        if cfg.min.is_zero() {
            cfg.min = cfg.frame_dur;
        }
        if cfg.max.is_zero() {
            cfg.max = Duration::from_millis(200);
        }
        if cfg.slack <= 0 {
            cfg.slack = 2;
        }
        if cfg.drift_ticks <= 0 {
            cfg.drift_ticks = 25;
        }
        let mut p = Pacer {
            min_fr: 0,
            max_fr: 0,
            inner: Mutex::new(Inner {
                q: VecDeque::new(),
                first_at: 0,
                last_at: 0,
                have_last: false,
                primed: false,
                started: false,
                target_fr: 0,
                drift_run: 0,
                ticks: 0,
                jitter: 0.0,
                st: Stats::default(),
                runner_set: false,
            }),
            run: Arc::new(Mutex::new(false)),
            epoch: Instant::now(),
            wake: Arc::new(tokio::sync::Notify::new()),
            cfg,
        };
        p.min_fr = p.frames(ns(p.cfg.min));
        p.max_fr = p.frames(ns(p.cfg.max));
        if p.max_fr < p.min_fr {
            p.max_fr = p.min_fr;
        }
        let t = p.clamp(p.frames(ns(p.cfg.initial)));
        lock(&p.inner).target_fr = t;
        p
    }

    /// The time since this pacer's epoch: what `push` and `tick` want when the caller has no
    /// clock of its own (the loop `start` runs ticks on this clock).
    pub fn now(&self) -> Duration {
        self.epoch.elapsed()
    }

    fn frames(&self, d_ns: i64) -> i64 {
        let n = (d_ns as f64 / ns(self.cfg.frame_dur) as f64).ceil() as i64;
        n.max(1)
    }

    fn clamp(&self, n: i64) -> i64 {
        n.clamp(self.min_fr, self.max_fr)
    }

    /// `Push`: queues one frame that arrived at `now`.
    pub fn push(&self, now: Duration, frame: Vec<u8>) {
        let now = ns(now);
        let mut g = lock(&self.inner);
        g.st.frames_in += 1;
        if g.have_last {
            let gap = now - g.last_at;
            let gap_d = if gap > 0 {
                Duration::from_nanos(gap as u64)
            } else {
                Duration::ZERO
            };
            if gap_d > g.st.max_gap {
                g.st.max_gap = gap_d;
            }
            if gap > 100_000_000 {
                g.st.gaps_100 += 1;
            }
            // RFC 3550 A.8 with the nominal frame spacing as the expected transit difference.
            let d = ((gap - ns(self.cfg.frame_dur)) as f64 / 1e6).abs();
            g.jitter += (d - g.jitter) / 16.0;
        }
        g.last_at = now;
        g.have_last = true;
        if g.q.is_empty() {
            g.first_at = now;
        }
        g.q.push_back(frame);
        while g.q.len() as i64 > self.max_fr {
            // drop OLDEST
            g.q.pop_front();
            g.st.drops_oldest += 1;
            // the new head is not older than this push for priming purposes
            g.first_at = now;
        }
    }

    /// `Tick`: called once per frame duration with the current time; returns what to send.
    pub fn tick(&self, now: Duration) -> (Option<Vec<u8>>, Kind) {
        let now = ns(now);
        let mut g = lock(&self.inner);
        g.ticks += 1;
        if g.ticks.is_multiple_of(ADAPT_EVERY) {
            self.adapt(&mut g);
        }
        if !g.primed {
            let held = !g.q.is_empty() && now - g.first_at >= g.target_fr * ns(self.cfg.frame_dur);
            if g.q.len() as i64 >= g.target_fr || held {
                g.primed = true;
            }
        }
        if !g.primed {
            return self.silence_locked(&mut g);
        }
        if g.q.is_empty() {
            // underrun
            g.st.underruns += 1;
            g.primed = false;
            g.drift_run = 0;
            g.target_fr = self.clamp(g.target_fr + 1);
            return self.silence_locked(&mut g);
        }
        if g.q.len() as i64 > g.target_fr + self.cfg.slack {
            g.drift_run += 1;
            if g.drift_run >= self.cfg.drift_ticks {
                g.drift_run = 0;
                g.q.pop_front();
                g.st.drops_drift += 1;
            }
        } else {
            g.drift_run = 0;
        }
        // the slip above removes at most one frame of a queue longer than target + slack >= 3
        let frame = g.q.pop_front();
        g.started = true;
        g.st.frames_out += 1;
        (frame, Kind::Audio)
    }

    /// The concealment frame is cloned per silence tick (one allocation, at most 16 KiB, 25
    /// times a second while the sender is silent): accepted, because `emit` takes ownership of a
    /// `Vec` (R1-123).
    fn silence_locked(&self, g: &mut Inner) -> (Option<Vec<u8>>, Kind) {
        match (&self.cfg.silence, g.started) {
            (Some(s), true) => {
                g.st.silence_out += 1;
                (Some(s.clone()), Kind::Silence)
            }
            _ => (None, Kind::None),
        }
    }

    /// `adapt`: moves the target one frame toward frame + 4 * jitter (mean plus a deviation
    /// allowance).
    fn adapt(&self, g: &mut Inner) {
        let extra = (4.0 * g.jitter * 1e6) as i64;
        let want = self.clamp(self.frames(ns(self.cfg.frame_dur) + extra));
        match want.cmp(&g.target_fr) {
            std::cmp::Ordering::Greater => g.target_fr += 1,
            std::cmp::Ordering::Less => g.target_fr -= 1,
            std::cmp::Ordering::Equal => {}
        }
    }

    /// `Stats`: a snapshot.
    pub fn stats(&self) -> Stats {
        let g = lock(&self.inner);
        let mut s = g.st.clone();
        s.jitter_ms = g.jitter;
        s.depth = g.q.len() as i64;
        s.target = Duration::from_nanos((g.target_fr * ns(self.cfg.frame_dur)) as u64);
        s
    }

    /// `Start`: runs the pacing loop on a task: `emit` is called from that one task, once per
    /// frame duration, for every Audio and Silence tick. A tick that finds the loop more than a
    /// frame late resynchronises instead of firing a burst to catch up. Call [`Pacer::stop`] to
    /// end it; a second `start` does nothing. Needs a tokio runtime.
    pub fn start<F>(self: &Arc<Self>, emit: F)
    where
        F: Fn(Vec<u8>, Kind) + Send + Sync + 'static,
    {
        {
            let mut g = lock(&self.inner);
            if g.runner_set {
                return;
            }
            g.runner_set = true;
        }
        // The task holds only a Weak<Pacer>: dropping every other Arc ends the loop (R1-122; Go
        // leaks the goroutine, PARITY 12 fixed the same class for AudioSession). The strong
        // reference lives for one tick at a time.
        let weak = Arc::downgrade(self);
        let (run, wake) = (self.run.clone(), self.wake.clone());
        let (fd, epoch) = (self.cfg.frame_dur, self.epoch);
        tokio::spawn(async move {
            let mut next = Instant::now() + fd;
            loop {
                tokio::select! {
                    _ = tokio::time::sleep_until(next) => {}
                    _ = wake.notified() => {}
                }
                let now = Instant::now();
                {
                    let stopped = lock(&run);
                    if *stopped {
                        return;
                    }
                    if now < next {
                        // woken for nothing (a stale `stop` permit)
                        continue;
                    }
                    let Some(p) = weak.upgrade() else {
                        return;
                    };
                    let r = catch_unwind(AssertUnwindSafe(|| {
                        let (f, k) = p.tick(now - epoch);
                        if k != Kind::None {
                            if let Some(f) = f {
                                emit(f, k);
                            }
                        }
                    }));
                    if r.is_err() {
                        crate::panic_hook::err_line(
                            "portico-transport: pacer: the emit callback panicked; the pacer stops",
                        );
                        return;
                    }
                }
                next += fd;
                if now.saturating_duration_since(next) > fd {
                    next = now + fd;
                }
            }
        });
    }

    /// `Stop`: ends the loop; no `emit` runs after it returns. Safe to call twice and without
    /// `start`.
    pub fn stop(&self) {
        *lock(&self.run) = true;
        self.wake.notify_one();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FD: Duration = Duration::from_millis(40);

    fn t(n: u64) -> Duration {
        Duration::from_millis(n)
    }

    fn fr(n: u8) -> Vec<u8> {
        vec![n]
    }

    fn cfg() -> Config {
        Config {
            frame_dur: FD,
            initial: t(100),
            min: t(40),
            max: t(200),
            silence: Some(vec![0]),
            ..Config::default()
        }
    }

    // TestBurstIsPlayedOutEvenlyOnePerTickInOrder
    #[test]
    fn burst_is_played_out_evenly_one_per_tick_in_order() {
        let p = Pacer::new(cfg());
        // Five frames arrive in one burst at t=0 (ffmpeg flushed), then nothing.
        for i in 1..=5u8 {
            p.push(t(0), fr(i));
        }
        let mut got = Vec::new();
        for i in 0..5u64 {
            let (f, k) = p.tick(FD * (i as u32 + 1));
            assert_eq!(k, Kind::Audio, "tick {i}");
            got.push(f.unwrap()[0]);
        }
        assert_eq!(got, vec![1, 2, 3, 4, 5]);
        let s = p.stats();
        assert_eq!((s.frames_out, s.drops_oldest), (5, 0), "{s:?}");
    }

    // TestNothingIsSentBeforeAudioStartsAndPrimingHoldsTheFirstFrames
    #[test]
    fn nothing_is_sent_before_audio_starts_and_priming_holds_the_first_frames() {
        let p = Pacer::new(cfg()); // target 3 frames
        assert_eq!(p.tick(t(0)).1, Kind::None, "silence before the first frame");
        p.push(t(0), fr(1));
        p.push(FD, fr(2));
        assert_eq!(p.tick(FD).1, Kind::None, "played before priming"); // 2 frames < 3, held 40 ms < 120 ms
        p.push(FD * 2, fr(3));
        let (f, k) = p.tick(FD * 2);
        assert_eq!(
            (k, f.unwrap()[0]),
            (Kind::Audio, 1),
            "not primed with 3 frames"
        );
    }

    // TestALoneFrameIsPlayedAfterTheTargetDelay
    #[test]
    fn a_lone_frame_is_played_after_the_target_delay() {
        let p = Pacer::new(cfg());
        p.push(t(0), fr(9));
        assert_eq!(p.tick(FD).1, Kind::None, "early");
        let (f, k) = p.tick(FD * 3);
        assert_eq!(
            (k, f.unwrap()[0]),
            (Kind::Audio, 9),
            "a lone frame was stranded"
        );
    }

    // TestBacklogIsCappedByDroppingTheOldest
    #[test]
    fn backlog_is_capped_by_dropping_the_oldest() {
        let p = Pacer::new(cfg()); // cap 5 frames = 200 ms
        for i in 1..=9u8 {
            p.push(t(0), fr(i));
        }
        let s = p.stats();
        assert_eq!((s.depth, s.drops_oldest), (5, 4), "{s:?}");
        let (f, k) = p.tick(FD);
        assert_eq!((k, f.unwrap()[0]), (Kind::Audio, 5), "kept the wrong end"); // frames 1-4 were the oldest
    }

    // TestUnderrunSendsSilenceKeepsTheStreamContinuousAndReprimesWithALargerTarget
    #[test]
    fn underrun_sends_silence_keeps_the_stream_continuous_and_reprimes_with_a_larger_target() {
        let p = Pacer::new(cfg());
        for i in 1..=3u8 {
            p.push(t(0), fr(i));
        }
        let kinds: Vec<Kind> = (0..8u32).map(|i| p.tick(FD * (i + 1)).1).collect();
        use Kind::*;
        assert_eq!(
            kinds,
            vec![Audio, Audio, Audio, Silence, Silence, Silence, Silence, Silence]
        );
        let s = p.stats();
        assert_eq!(
            (s.underruns, s.silence_out, s.target),
            (1, 5, t(160)),
            "{s:?}"
        );
        // Re-primes: needs 4 frames now.
        for i in 10..13u8 {
            p.push(FD * 10, fr(i));
        }
        assert_eq!(p.tick(FD * 10).1, Silence, "resumed before re-priming");
        p.push(FD * 10, fr(13));
        let (f, k) = p.tick(FD * 11);
        assert_eq!((k, f.unwrap()[0]), (Audio, 10));
    }

    // TestNoSilenceWhenNoneConfigured
    #[test]
    fn no_silence_when_none_configured() {
        let mut c = cfg();
        c.silence = None;
        let p = Pacer::new(c);
        for i in 1..=3u8 {
            p.push(t(0), fr(i));
        }
        for i in 0..3u32 {
            p.tick(FD * (i + 1));
        }
        assert_eq!(p.tick(FD * 5).1, Kind::None);
    }

    // TestDriftSlipsOneOldestFrameAfterTheExcessPersists
    #[test]
    fn drift_slips_one_oldest_frame_after_the_excess_persists() {
        let mut c = cfg();
        c.initial = t(80); // target 2 frames, slack 2: excess means depth > 4
        c.drift_ticks = 5;
        let p = Pacer::new(c);
        // The producer is slightly fast: a standing surplus of 5 frames that one-in-one-out never drains.
        for i in 0..5u8 {
            p.push(t(0), fr(i));
        }
        for i in 0..12u32 {
            let now = FD * (i + 1);
            p.tick(now);
            p.push(now, fr(100 + i as u8));
        }
        let s = p.stats();
        // one slip brings the depth back inside target+slack, then it holds
        assert_eq!((s.drops_drift, s.depth), (1, 4), "slips: {s:?}");
        assert_eq!((s.underruns, s.drops_oldest), (0, 0), "{s:?}");
    }

    // TestTargetAdaptsUpWithJitterAndBackDownWhenCalm
    #[test]
    fn target_adapts_up_with_jitter_and_back_down_when_calm() {
        let mut c = cfg();
        c.initial = t(40);
        let p = Pacer::new(c);
        let mut now = t(0);
        // Alternating 5 ms / 75 ms gaps: ~35 ms deviation each -> jitter ~35, wants 40+140 -> 5 frames.
        for i in 0..200 {
            now += if i % 2 == 0 { t(5) } else { t(75) };
            p.push(now, fr(1));
            p.tick(now);
        }
        let hi = p.stats();
        assert!(
            hi.target >= t(160) && hi.jitter_ms >= 20.0,
            "did not adapt up: {hi:?}"
        );
        for _ in 0..600 {
            now += FD;
            p.push(now, fr(1));
            p.tick(now);
        }
        let lo = p.stats();
        assert!(
            lo.target < hi.target && lo.target <= t(80),
            "did not adapt down: before {hi:?} after {lo:?}"
        );
    }

    // TestArrivalGapsAreCounted
    #[test]
    fn arrival_gaps_are_counted() {
        let p = Pacer::new(cfg());
        p.push(t(0), fr(1));
        p.push(t(40), fr(2));
        p.push(t(300), fr(3));
        let s = p.stats();
        assert_eq!(
            (s.max_gap, s.gaps_100, s.frames_in),
            (t(260), 1, 3),
            "{s:?}"
        );
        assert!(!s.to_string().is_empty());
    }

    // TestStartEmitsEvenlyAndStopsCleanly: the real loop. A burst of 6 frames in, even spacing
    // out (generous tolerances: this is a clock test).
    #[tokio::test(start_paused = true)]
    async fn start_emits_evenly_and_stops_cleanly() {
        let c = Config {
            frame_dur: t(20),
            initial: t(40),
            min: t(20),
            max: t(200),
            ..Config::default()
        };
        let p = Arc::new(Pacer::new(c));
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        p.start(move |_, _| {
            let _ = tx.send(Instant::now());
        });
        let t0 = Duration::ZERO;
        for i in 0..6u8 {
            p.push(t0, fr(i));
        }
        let mut ts = Vec::new();
        while ts.len() < 6 {
            match tokio::time::timeout(Duration::from_secs(2), rx.recv()).await {
                Ok(Some(x)) => ts.push(x),
                _ => panic!("only {} frames", ts.len()),
            }
        }
        p.stop();
        p.stop();
        for i in 1..ts.len() {
            let d = ts[i] - ts[i - 1];
            assert_eq!(d, t(20), "uneven spacing {d:?} at {i}"); // paused clock: exact
        }
        // anything emitted before stop returned is allowed; nothing after
        tokio::time::sleep(t(30)).await;
        while rx.try_recv().is_ok() {}
        tokio::time::sleep(t(100)).await;
        assert!(rx.try_recv().is_err(), "emit after stop");
    }

    // R2-18: a loop that wakes more than a frame late resynchronises: ONE tick for the jump, then
    // one frame apart again. Without the resync `next += fd` stays behind `now` and the loop fires
    // every missed frame at once (a burst at zero spacing, which the pacer exists to prevent).
    #[tokio::test(start_paused = true)]
    async fn a_loop_that_wakes_late_resynchronises_without_a_burst() {
        let c = Config {
            frame_dur: t(20),
            initial: t(40),
            min: t(20),
            max: t(200),
            ..Config::default()
        };
        let p = Arc::new(Pacer::new(c));
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        p.start(move |_, _| {
            let _ = tx.send(Instant::now());
        });
        for i in 0..10u8 {
            p.push(Duration::ZERO, fr(i));
        }
        for _ in 0..5 {
            tokio::task::yield_now().await;
        }
        assert!(
            rx.try_recv().is_err(),
            "nothing is due before the first frame time"
        );
        let t0 = Instant::now();
        tokio::time::advance(t(100)).await; // 5 frames in one step: the stalled runtime
        for _ in 0..20 {
            tokio::task::yield_now().await;
        }
        let mut burst = Vec::new();
        while let Ok(x) = rx.try_recv() {
            burst.push(x);
        }
        assert_eq!(
            burst.len(),
            1,
            "one tick for the jump, not a burst: {burst:?}"
        );
        assert_eq!(burst[0], t0 + t(100));
        // and the next ones are a frame apart
        tokio::time::sleep(t(20)).await;
        tokio::task::yield_now().await;
        tokio::time::sleep(t(20)).await;
        tokio::task::yield_now().await;
        let mut after = Vec::new();
        while let Ok(x) = rx.try_recv() {
            after.push(x);
        }
        assert_eq!(after, vec![t0 + t(120), t0 + t(140)]);
        p.stop();
    }

    // Rust-only: a panicking emit ends the loop, not the process, and stop without start is fine.
    #[tokio::test]
    async fn a_panicking_emit_ends_the_loop_and_stop_without_start_is_fine() {
        Pacer::new(cfg()).stop();
        let c = Config {
            frame_dur: t(10),
            initial: t(10),
            min: t(10),
            max: t(100),
            ..Config::default()
        };
        let p = Arc::new(Pacer::new(c));
        let n = Arc::new(std::sync::atomic::AtomicU32::new(0));
        let n2 = n.clone();
        p.start(move |_, _| {
            n2.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            panic!("test panic in emit");
        });
        for i in 0..5u8 {
            p.push(t(0), fr(i));
        }
        tokio::time::sleep(t(200)).await;
        assert_eq!(
            n.load(std::sync::atomic::Ordering::SeqCst),
            1,
            "emit ran again after it panicked"
        );
        p.stop();
    }

    // R1-122: the loop holds no strong reference; dropping every Arc ends it and frees `emit`.
    #[tokio::test]
    async fn dropping_every_arc_ends_the_loop() {
        let c = Config {
            frame_dur: t(10),
            initial: t(10),
            min: t(10),
            max: t(100),
            ..Config::default()
        };
        let p = Arc::new(Pacer::new(c));
        let sentinel = Arc::new(());
        let s2 = sentinel.clone();
        p.start(move |_, _| {
            let _keep = &s2;
        });
        drop(p);
        let end = tokio::time::Instant::now() + Duration::from_secs(2);
        while Arc::strong_count(&sentinel) > 1 && tokio::time::Instant::now() < end {
            tokio::time::sleep(t(10)).await;
        }
        assert_eq!(
            Arc::strong_count(&sentinel),
            1,
            "the loop outlived the pacer"
        );
    }

    // Rust-only: a second start does nothing, and no emit runs after stop returned (20 frames
    // pushed, stop at once).
    #[tokio::test]
    async fn a_second_start_is_ignored_and_stop_is_final() {
        let c = Config {
            frame_dur: t(10),
            initial: t(10),
            min: t(10),
            max: t(400),
            ..Config::default()
        };
        let p = Arc::new(Pacer::new(c));
        let n = Arc::new(std::sync::atomic::AtomicU32::new(0));
        let (a, b) = (n.clone(), n.clone());
        p.start(move |_, _| {
            a.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        });
        p.start(move |_, _| {
            b.fetch_add(1000, std::sync::atomic::Ordering::SeqCst);
        });
        for i in 0..20u8 {
            p.push(t(0), fr(i));
        }
        tokio::time::sleep(t(60)).await;
        p.stop();
        let at_stop = n.load(std::sync::atomic::Ordering::SeqCst);
        assert!(at_stop < 1000, "the second start ran: {at_stop}");
        tokio::time::sleep(t(100)).await;
        assert_eq!(n.load(std::sync::atomic::Ordering::SeqCst), at_stop);
    }
}
