//! `talk.go`: the `unifi_protect_talk` driver (front-door talk plan, T3).
//!
//! A doorbell whose RING is detected elsewhere (Python's `ha_event` source keeps owning the ring
//! and the phones) but whose ANSWER is carried here: the driver is told "call X is ringing"
//! through the IPC verb `external_ring`, holds that call answerable for a bounded time, and, once
//! a client leg has connected, opens the talkback media in-process ([`ConnectAnswerer`], the shape
//! `gate_sip_server` uses), so the hub's `join_call_to_browser` bridges the audio unchanged.
//!
//! The media is [`talk_media`](super::talk_media) (the guard, the console's talkback session, the
//! uplink), [`talk_down`](super::talk_down) (the visitor's voice) and a call out is
//! [`talk_dial`](super::talk_dial). Door unlock is out of scope: capabilities carry `HasRelay =
//! false` and `OpenRelay` is refused.
//!
//! One call per door. A ring is answered by the FIRST `answer_with_audio`; a second answerer gets
//! `AlreadyAnswered`. A failed open leaves the call ringing.
//!
//! The shape of the port follows `ring.rs`: one `Core` behind `Arc`, `st` (Go's `mu`)
//! and `emit` (Go's `emitMu`, taken first), neither held across an `.await`, timers as tasks that
//! are aborted when the call ends, a drop guard for an `answer` future that is dropped. The
//! package variables the Go tests shrink are per-driver [`TalkTiming`]. PARITY.md 136, 143 to 146.

use crate::util::lock;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use serde_json::{Map, Value};
use tokio::task::JoinHandle;
use tokio::time::Instant;

use super::ffmpeg::{self, arg_safe, scrub_line};
use super::talk_down::{TalkHealth, TalkStats};
use crate::cancel::CancelToken;
use crate::driver::{
    AudioClaimer, AudioTopology, BoxFuture, CallAudio, CallDialer, CallHangupper, Capabilities,
    ConnectAnswerer, Driver, DriverError, EventSink, ExternalRinger, MediaTarget,
    ReasonedHangupper, Sentinel, TalkHealther, STATE_ACTIVE, STATE_ANSWERED, STATE_ENDED,
};
use crate::gostd::url as gourl;
use crate::panic_hook::log_line;

pub const KEY: &str = "unifi_protect_talk";

/// The package variables of `talk.go`, `talk_media.go` and `talk_dial.go` that tests shrink,
/// per driver (the tests run in parallel).
#[derive(Clone, Copy, Debug)]
pub struct TalkTiming {
    /// `external_ring` without a hold
    pub default_hold: Duration,
    pub min_hold: Duration,
    pub max_hold: Duration,
    /// an answered session nobody attached to is closed after this (bell-talk's grace)
    pub orphan: Duration,
    /// the session guard's whole exchange
    pub guard_timeout: Duration,
    /// a dialled session whose phone has not connected its audio leg is closed after this
    pub dial_orphan: Duration,
    /// the longest a call out lasts
    pub dial_max: Duration,
    /// how long `stop` / a close waits for a media's subprocesses to be reaped (Go waits for ever)
    pub close_wait: Duration,
}

impl Default for TalkTiming {
    fn default() -> Self {
        TalkTiming {
            default_hold: Duration::from_secs(30),
            min_hold: Duration::from_secs(1),
            max_hold: Duration::from_secs(120),
            orphan: Duration::from_secs(10),
            guard_timeout: Duration::from_secs(3),
            dial_orphan: Duration::from_secs(20),
            dial_max: Duration::from_secs(300),
            close_wait: Duration::from_secs(5),
        }
    }
}

/// `talkConfig`.
#[derive(Clone, Default)]
pub struct TalkConfig {
    pub device_id: String,
    pub host: String,
    /// the Protect API key: never logged, never in an error
    pub key: String,
    pub camera_id: String,
    pub downlink_url: String,
    pub guard_url: String,
    pub verify_ssl: bool,
    pub gain: i64,
    /// outbound dial-out (`talk_dial.rs`): on unless the entry says `dial_enabled: false`
    pub dial_enabled: bool,
    /// captured at configure, like `UnifiProtect`'s
    pub scheme: String,
    /// Go's `ffmpegBinary`, captured at configure
    pub ffmpeg: String,
}

/// `talkMedia`: one open talkback media session as the driver sees it. The audio is the 16 kHz
/// L16 mono pair of [`CallAudio`]. `close` ends everything the session opened (idempotent, does
/// not wait: [`TalkMedia::closed`] resolves once it is over); `done` is set when it is over for
/// any reason.
pub trait TalkMedia: CallAudio {
    fn close(&self);
    fn closed(&self) -> BoxFuture<'_, ()>;
    fn done(&self) -> CancelToken;
    /// `endReasoner`: why the media ended on its own, if it knows.
    fn end_reason(&self) -> Option<String> {
        None
    }
    /// `orphanGracer`: re-arms the unattached-session cap (an outbound call gives the phone
    /// longer to connect).
    fn set_orphan_grace(&self, _d: Duration) {}
}

/// What an opener may read of the driver (`openTalkMedia(ctx, d, callID)` reads `d.cfg`, `d.ctx`
/// and sets `d.stats`).
#[derive(Clone)]
pub struct TalkEnv {
    pub cfg: TalkConfig,
    pub timing: TalkTiming,
    /// the driver's run context
    pub run: CancelToken,
    stats: Arc<Mutex<Option<Arc<TalkStats>>>>,
}

impl TalkEnv {
    /// `d.stats = st`.
    pub fn set_stats(&self, st: Arc<TalkStats>) {
        *lock(&self.stats) = Some(st);
    }
}

/// `talkOpener`: opens the media of one call. Called only once a client leg has connected (or
/// for a call out, at the dial). `noBackendOpener` of Go is a test double here.
pub trait TalkOpener: Send + Sync {
    fn open<'a>(
        &'a self,
        ctx: &'a CancelToken,
        env: TalkEnv,
        call_id: &'a str,
    ) -> BoxFuture<'a, Result<Arc<dyn TalkMedia>, DriverError>>;
}

/// The opener that opens nothing (`noBackendOpener`, T3's stub kept for tests).
pub struct NoBackendOpener;

impl TalkOpener for NoBackendOpener {
    fn open<'a>(
        &'a self,
        _ctx: &'a CancelToken,
        _env: TalkEnv,
        _call_id: &'a str,
    ) -> BoxFuture<'a, Result<Arc<dyn TalkMedia>, DriverError>> {
        Box::pin(async {
            Err(DriverError::wrapping(
                Sentinel::NotSupported,
                "unifi_protect_talk: the talkback media backend is not built yet: not_supported",
            ))
        })
    }
}

/// `talkCall`: the one call this door can hold.
pub(crate) struct TalkCall {
    /// identity for timers and the answer path (Go compares the pointer)
    pub serial: u64,
    pub id: String,
    pub deadline: Instant,
    pub timer: Option<JoinHandle<()>>,
    pub answering: bool,
    pub answered: bool,
    pub media: Option<Arc<dyn TalkMedia>>,
    /// placed by `dial_call`, not rung by a visitor
    pub outbound: bool,
    /// outbound only: the pair `claimed_audio` hands to the browser leg
    pub audio: Option<Arc<dyn CallAudio>>,
    /// outbound only: the call's duration cap
    pub cap_timer: Option<JoinHandle<()>>,
}

impl TalkCall {
    pub fn new(serial: u64, id: &str) -> TalkCall {
        TalkCall {
            serial,
            id: id.to_string(),
            deadline: Instant::now(),
            timer: None,
            answering: false,
            answered: false,
            media: None,
            outbound: false,
            audio: None,
            cap_timer: None,
        }
    }

    /// `stop`: both timers.
    pub fn stop(&self) {
        if let Some(h) = &self.timer {
            h.abort();
        }
        if let Some(h) = &self.cap_timer {
            h.abort();
        }
    }
}

pub(crate) struct St {
    pub run: Option<CancelToken>,
    pub events: Option<Arc<dyn EventSink>>,
    pub stopped: bool,
    pub call: Option<TalkCall>,
    /// why the last call failed to open or ended (`Health.LastEnd`)
    pub last_end: String,
    pub open: Arc<dyn TalkOpener>,
}

/// The driver's shared core (what the timers, the watchers and the hub-facing methods use).
pub(crate) struct TalkCore {
    pub cfg: TalkConfig,
    pub timing: TalkTiming,
    /// `d.mu`
    pub st: Mutex<St>,
    /// `emitMu`: serialises what is reported to the sink (taken before `st`). Never held across
    /// an `.await`.
    pub emit: Mutex<()>,
    /// the current (or last) call's counters
    pub stats: Arc<Mutex<Option<Arc<TalkStats>>>>,
    pub serial: AtomicU64,
}

/// Go's `TalkDriver`.
pub struct TalkDriver {
    pub(crate) core: Arc<TalkCore>,
    scheme: String,
    timing: TalkTiming,
    ffmpeg: String,
    opener: Option<Arc<dyn TalkOpener>>,
}

/// `NewTalk`.
pub fn new() -> Box<dyn Driver> {
    Box::new(TalkDriver::default())
}

fn default_opener() -> Arc<dyn TalkOpener> {
    Arc::new(super::talk_media::RealOpener)
}

impl TalkCore {
    fn new(cfg: TalkConfig, timing: TalkTiming, open: Arc<dyn TalkOpener>) -> TalkCore {
        TalkCore {
            cfg,
            timing,
            st: Mutex::new(St {
                run: None,
                events: None,
                stopped: false,
                call: None,
                last_end: String::new(),
                open,
            }),
            emit: Mutex::new(()),
            stats: Arc::new(Mutex::new(None)),
            serial: AtomicU64::new(0),
        }
    }

    pub(crate) fn env(&self, st: &St) -> TalkEnv {
        TalkEnv {
            cfg: self.cfg.clone(),
            timing: self.timing,
            run: st.run.clone().unwrap_or_default(),
            stats: self.stats.clone(),
        }
    }

    pub(crate) fn next_serial(&self) -> u64 {
        self.serial.fetch_add(1, Ordering::SeqCst) + 1
    }

    /// `note`: records why a call failed to open or ended, and tells the sink if it listens
    /// ([`CallReasonSink`](crate::driver::CallReasonSink)). The reason is scrubbed: one short
    /// line, no URL.
    pub(crate) fn note(&self, call_id: &str, kind: &str, reason: &str) {
        let reason = scrub_line(reason);
        let (sink, stopped) = {
            let mut st = lock(&self.st);
            st.last_end = reason.clone();
            (st.events.clone(), st.stopped)
        };
        if let Some(sink) = sink {
            if !stopped {
                if let Some(rs) = sink.call_reason_sink() {
                    rs.call_reason(&self.cfg.device_id, call_id, kind, &reason);
                }
            }
        }
    }

    /// A timer that ends the ring `serial` after `wait`. A panic in the end is logged (scrubbed)
    /// and contained.
    pub(crate) fn arm(self: &Arc<Self>, serial: u64, wait: Duration) -> JoinHandle<()> {
        let core = self.clone();
        tokio::spawn(async move {
            tokio::time::sleep(wait).await;
            core.guarded("ending a ring", |c| c.expire(serial));
        })
    }

    /// Runs `f` under `catch_unwind`; a panic is logged with its scrubbed payload.
    pub(crate) fn guarded(self: &Arc<Self>, what: &str, f: impl FnOnce(&Arc<Self>)) {
        use std::panic::{catch_unwind, AssertUnwindSafe};
        if let Err(p) = catch_unwind(AssertUnwindSafe(|| f(self))) {
            log_line(&format!(
                "portico-transport: {KEY}: panic {what}: {}",
                ffmpeg::scrub_all(
                    &crate::util::panic_message(&*p, "<non-string payload>"),
                    &self.cfg.key
                )
            ));
        }
    }

    /// `ExternalRing`: registers `call_id` as the ringing call for `hold` (clamped to
    /// `[min_hold, max_hold]`; zero means `default_hold`). The phones were told by the caller,
    /// so nothing is reported to the sink now. The same call again changes nothing. A different
    /// call while the held one is only ringing (not answering, not answered) SUPERSEDES it: a
    /// visitor pressing again must not lock the door out. The old call's timer is stopped and it
    /// is reported ended through the same path an expiry uses (reason "superseded by a new
    /// ring"); the new call gets a fresh hold. A different call while one is answering or
    /// answered is `Busy`.
    pub(crate) fn external_ring(
        self: &Arc<Self>,
        call_id: &str,
        hold: Duration,
    ) -> Result<(), DriverError> {
        if call_id.is_empty() {
            return Err(DriverError::new(
                "unifi_protect_talk: external_ring needs a call_id",
            ));
        }
        let t = &self.timing;
        let mut hold = if hold.is_zero() { t.default_hold } else { hold };
        if hold < t.min_hold {
            hold = t.min_hold;
        }
        if hold > t.max_hold {
            hold = t.max_hold;
        }
        let _emit = lock(&self.emit);
        let mut st = lock(&self.st);
        if st.stopped || st.events.is_none() {
            return Err(DriverError::wrapping(
                Sentinel::NotSupported,
                "unifi_protect_talk: not started: not_supported",
            ));
        }
        let mut superseded = false;
        if let Some(c) = &st.call {
            if c.id == call_id {
                return Ok(());
            }
            if c.answered || c.answering {
                return Err(DriverError::wrapping(
                    Sentinel::Busy,
                    "unifi_protect_talk: already holding a call: busy",
                ));
            }
            superseded = true;
        }
        let serial = self.next_serial();
        let mut c = TalkCall::new(serial, call_id);
        c.deadline = Instant::now() + hold;
        c.timer = Some(self.arm(serial, hold));
        let old = st.call.replace(c);
        if !superseded {
            return Ok(());
        }
        // Supersede: stop the old ring's timer, make the new call the held one at once (so an
        // answer for it is never refused), then report the old as ended the way expire does.
        // `emit` is held throughout, so reports do not interleave.
        let old = old.expect("superseded");
        old.stop();
        let sink = st.events.clone();
        drop(st);
        if let Some(sink) = sink {
            sink.state_changed(&old.id, STATE_ENDED);
        }
        self.note(&old.id, "ended", "superseded by a new ring");
        Ok(())
    }

    /// `expire`: ends the call `serial` if it is still ringing. A call being answered is left to
    /// the answer, which re-arms the timer if the open fails.
    pub(crate) fn expire(self: &Arc<Self>, serial: u64) {
        let _emit = lock(&self.emit);
        let st = lock(&self.st);
        match st.call.as_ref() {
            Some(c) if c.serial == serial && !c.answered && !c.answering => {}
            _ => return,
        }
        if let Some(c) = self.end_locked(st) {
            self.note(&c.id, "ended", "the ring expired unanswered");
        }
    }

    /// `endLocked`: clears the call and reports it ended. Called with `emit` and `st` held;
    /// releases `st` (not `emit`) before reporting. Returns the call.
    pub(crate) fn end_locked(&self, mut st: MutexGuard<'_, St>) -> Option<TalkCall> {
        let c = st.call.take()?;
        c.stop();
        let (sink, stopped) = (st.events.clone(), st.stopped);
        drop(st);
        if let Some(sink) = sink {
            if !stopped {
                sink.state_changed(&c.id, STATE_ENDED);
            }
        }
        Some(c)
    }

    /// Is the held call `id` the one with this `serial`?
    pub(crate) fn holds_serial(&self, id: &str, serial: u64) -> bool {
        lock(&self.st)
            .call
            .as_ref()
            .is_some_and(|c| c.id == id && c.serial == serial)
    }

    /// `RingingCall`: is `call_id` held, ringing, and unanswered?
    pub(crate) fn ringing_call(&self, call_id: &str) -> bool {
        lock(&self.st)
            .call
            .as_ref()
            .is_some_and(|c| c.id == call_id && !c.answered && !c.answering)
    }

    /// `AnswerWithAudio`: answers `call_id` now (a client leg has connected) and returns its
    /// audio, already taken by the caller. `NoSuchCall` for a call not held, `AlreadyAnswered`
    /// for one taken (the first answerer keeps it). A failed open leaves the call ringing for
    /// another attempt until its hold runs out.
    pub(crate) async fn answer_with_audio(
        self: &Arc<Self>,
        ctx: &CancelToken,
        call_id: &str,
    ) -> Result<Arc<dyn CallAudio>, DriverError> {
        let (serial, open, env) = {
            let mut st = lock(&self.st);
            let (serial, open) = match st.call.as_mut() {
                Some(c) if c.id == call_id => {
                    if c.answered || c.answering {
                        return Err(DriverError::wrapping(
                            Sentinel::AlreadyAnswered,
                            format!(
                                "unifi_protect_talk: {call_id} is already answered: already_answered"
                            ),
                        ));
                    }
                    c.answering = true;
                    c.stop();
                    (c.serial, st.open.clone())
                }
                _ => {
                    return Err(DriverError::wrapping(
                        Sentinel::NoSuchCall,
                        format!("unifi_protect_talk: {call_id} is not a live call: no_such_call"),
                    ))
                }
            };
            (serial, open, self.env(&st))
        };
        let mut guard = Answering {
            core: self.clone(),
            serial,
            armed: true,
        };
        let opened = open.open(ctx, env, call_id).await;
        guard.armed = false;
        let m = match opened {
            Ok(m) => m,
            Err(e) => {
                self.note(call_id, "answer_failed", e.message());
                self.release_answering(serial, false);
                return Err(e);
            }
        };

        let reported = {
            let _emit = lock(&self.emit);
            let mut st = lock(&self.st);
            let live = st.call.as_ref().is_some_and(|c| c.serial == serial) && !st.stopped;
            if !live {
                None
            } else {
                let (id, sink) = {
                    let c = st.call.as_mut().expect("checked live");
                    c.answering = false;
                    c.answered = true;
                    c.media = Some(m.clone());
                    (c.id.clone(), st.events.clone())
                };
                drop(st);
                if let Some(sink) = sink {
                    sink.state_changed(&id, STATE_ANSWERED);
                    sink.state_changed(&id, STATE_ACTIVE);
                }
                Some(id)
            }
        };
        let Some(id) = reported else {
            // hung up or stopped while the media opened
            self.close_and_wait(&m).await;
            return Err(DriverError::wrapping(
                Sentinel::NoSuchCall,
                format!("unifi_protect_talk: {call_id} ended while answering: no_such_call"),
            ));
        };

        self.watch_media(serial, id.clone(), m.clone());
        Ok(Arc::new(TalkAudio {
            media: m,
            core: self.clone(),
            call_id: id,
        }))
    }

    /// The media ending on its own ends the call.
    pub(crate) fn watch_media(
        self: &Arc<Self>,
        serial: u64,
        call_id: String,
        m: Arc<dyn TalkMedia>,
    ) {
        let core = self.clone();
        ffmpeg::spawn_guarded(
            "unifi_protect_talk media watcher",
            self.cfg.key.clone(),
            async move {
                m.done().cancelled().await;
                if let Some(r) = m.end_reason() {
                    if !r.is_empty() {
                        core.note(&call_id, "ended", &r);
                    }
                }
                core.guarded("ending an answered call", |c| c.end_answered(serial));
            },
        );
    }

    /// `endAnswered`: ends `serial`'s call once its media is gone.
    pub(crate) fn end_answered(self: &Arc<Self>, serial: u64) {
        let _emit = lock(&self.emit);
        let st = lock(&self.st);
        if !st.call.as_ref().is_some_and(|c| c.serial == serial) {
            return;
        }
        self.end_locked(st);
    }

    /// A failed or abandoned answer: `answering` is cleared and the hold timer re-armed with what
    /// is left of the hold; if the hold ran out while opening, the call ends now (`spawn`: on a
    /// task, because this runs from a `Drop`).
    pub(crate) fn release_answering(self: &Arc<Self>, serial: u64, spawn: bool) {
        let overdue = {
            let mut st = lock(&self.st);
            match st.call.as_mut() {
                Some(c) if c.serial == serial && !c.answered => {
                    c.answering = false;
                    let left = c.deadline.saturating_duration_since(Instant::now());
                    if !left.is_zero() {
                        c.timer = Some(self.arm(serial, left));
                        return;
                    }
                    true
                }
                _ => true,
            }
        };
        if !overdue {
            return;
        }
        if spawn {
            if let Ok(h) = tokio::runtime::Handle::try_current() {
                let core = self.clone();
                h.spawn(async move {
                    core.guarded("ending a ring", |c| c.expire(serial));
                });
            }
        } else {
            self.expire(serial); // the hold ran out while opening
        }
    }

    /// `m.Close()` and the wait for it, bounded.
    pub(crate) async fn close_and_wait(&self, m: &Arc<dyn TalkMedia>) {
        m.close();
        let _ = tokio::time::timeout(self.timing.close_wait, m.closed()).await;
    }

    /// `HangupCallWhy`: ends `call_id`, ringing or answered, and closes its media. The end note
    /// reads "hung up (<why>)", or plain "hung up" with no reason.
    pub(crate) fn hangup_call_why(
        self: &Arc<Self>,
        call_id: &str,
        why: &str,
    ) -> Result<(), DriverError> {
        let emit = lock(&self.emit);
        let st = lock(&self.st);
        if !st.call.as_ref().is_some_and(|c| c.id == call_id) {
            drop(st);
            drop(emit);
            return Err(DriverError::wrapping(
                Sentinel::NoSuchCall,
                format!("unifi_protect_talk: {call_id}: no_such_call"),
            ));
        }
        let c = self.end_locked(st);
        drop(emit);
        let mut note = String::from("hung up");
        let why = ffmpeg::go_trim_space(why);
        if !why.is_empty() {
            note.push_str(&format!(" ({why})"));
        }
        self.note(call_id, "ended", &note);
        if let Some(m) = c.and_then(|c| c.media) {
            m.close();
        }
        Ok(())
    }

    /// `Health`: the door's diagnostics block (the zero value before any call).
    pub(crate) fn health(&self) -> TalkHealth {
        let end = lock(&self.st).last_end.clone();
        let s = lock(&self.stats).clone();
        match s {
            None => TalkHealth {
                last_end: end,
                ..TalkHealth::default()
            },
            Some(s) => {
                let mut h = s.snapshot();
                h.last_end = end;
                h
            }
        }
    }
}

/// Gives the ring back if the `answer_with_audio` future is dropped while the media opens
/// (Go's cancelled context does the same).
struct Answering {
    core: Arc<TalkCore>,
    serial: u64,
    armed: bool,
}

impl Drop for Answering {
    fn drop(&mut self) {
        if self.armed {
            self.core.release_answering(self.serial, true);
        }
    }
}

/// `talkAudio`: the media as the hub holds it: a client leg going away (`detach`) hangs the
/// call up.
pub(crate) struct TalkAudio {
    pub media: Arc<dyn TalkMedia>,
    pub core: Arc<TalkCore>,
    pub call_id: String,
}

impl CallAudio for TalkAudio {
    fn input(&self) -> tokio::sync::mpsc::Sender<Vec<u8>> {
        self.media.input()
    }
    fn output(&self) -> Arc<tokio::sync::Mutex<tokio::sync::mpsc::Receiver<Vec<u8>>>> {
        self.media.output()
    }
    fn attach(&self) {
        self.media.attach();
    }
    fn detach(&self) {
        self.media.detach();
        let _ = self.core.hangup_call_why(&self.call_id, "detach");
    }
}

impl Default for TalkDriver {
    fn default() -> Self {
        let timing = TalkTiming::default();
        TalkDriver {
            core: Arc::new(TalkCore::new(
                TalkConfig::default(),
                timing,
                default_opener(),
            )),
            scheme: "https".to_string(),
            timing,
            ffmpeg: ffmpeg::FFMPEG_BINARY.to_string(),
            opener: None,
        }
    }
}

impl TalkDriver {
    /// Go's package variable `scheme`: set before `configure`, which captures it.
    #[cfg(feature = "test-seams")]
    pub fn set_scheme(&mut self, scheme: &str) {
        self.scheme = scheme.to_string();
    }

    /// The timing variables the Go tests shrink, set before `configure`.
    #[cfg(feature = "test-seams")]
    pub fn set_timing(&mut self, t: TalkTiming) {
        self.timing = t;
    }

    /// Go's package variable `ffmpegBinary`: set before `configure`, which captures it.
    #[cfg(feature = "test-seams")]
    pub fn set_ffmpeg_binary(&mut self, path: &str) {
        self.ffmpeg = path.to_string();
    }

    /// Go's `d.open = ...`: replaces the media opener (tests). Survives a later `configure`.
    #[cfg(feature = "test-seams")]
    pub fn set_opener(&mut self, o: Arc<dyn TalkOpener>) {
        lock(&self.core.st).open = o.clone();
        self.opener = Some(o);
    }

    /// Go's `d.cfg.downlinkURL = ""` and the like: edits the configuration the opener sees
    /// (tests; before `start`).
    #[cfg(feature = "test-seams")]
    pub fn edit_config_for_test(&mut self, f: impl FnOnce(&mut TalkConfig)) {
        let mut cfg = self.core.cfg.clone();
        f(&mut cfg);
        let open = lock(&self.core.st).open.clone();
        self.core = Arc::new(TalkCore::new(cfg, self.timing, open));
    }

    /// The deadline of the held call (`d.call.deadline`), for the hold tests.
    #[cfg(feature = "test-seams")]
    pub fn deadline_for_test(&self) -> Option<Instant> {
        lock(&self.core.st).call.as_ref().map(|c| c.deadline)
    }

    /// Go's `d.call.answered = false; d.call.answering = true` (a state the busy test forces).
    #[cfg(feature = "test-seams")]
    pub fn force_answering_for_test(&self) {
        if let Some(c) = lock(&self.core.st).call.as_mut() {
            c.answered = false;
            c.answering = true;
        }
    }

    /// `d.lastEnd`.
    #[cfg(feature = "test-seams")]
    pub fn last_end_for_test(&self) -> String {
        lock(&self.core.st).last_end.clone()
    }

    /// The media of the held call (`d.call.media`).
    #[cfg(feature = "test-seams")]
    pub fn media_for_test(&self) -> Option<Arc<dyn TalkMedia>> {
        lock(&self.core.st)
            .call
            .as_ref()
            .and_then(|c| c.media.clone())
    }

    /// The counters of the current (or last) call (`d.stats`).
    #[cfg(feature = "test-seams")]
    pub fn stats_for_test(&self) -> Option<Arc<TalkStats>> {
        lock(&self.core.stats).clone()
    }

    /// `Health`.
    pub fn health(&self) -> TalkHealth {
        self.core.health()
    }

    /// `RingingCall`.
    pub fn ringing_call(&self, call_id: &str) -> bool {
        self.core.ringing_call(call_id)
    }

    /// Is a call out held (`ClaimedAudio` found)?
    #[cfg(feature = "test-seams")]
    pub fn claimed_audio_for_test(&self, id: &str) -> bool {
        self.core.claimed_audio(id).is_some()
    }

    /// `HangupCallWhy`.
    pub fn hangup_call_why(&self, call_id: &str, why: &str) -> Result<(), DriverError> {
        self.core.hangup_call_why(call_id, why)
    }
}

fn str_of(cfg: &Map<String, Value>, k: &str) -> String {
    match cfg.get(k) {
        Some(Value::String(s)) => s.clone(),
        _ => String::new(),
    }
}

impl Driver for TalkDriver {
    /// Validates the entry's fields; it touches no network.
    ///
    /// Keys: `device_id`, `protect_host` (bare host or host:port), `protect_api_key`,
    /// `camera_id` (all required); `downlink_url` (rtsp:// or rtsps://), `guard_url` (http:// or
    /// https://) (both optional here, checked where used), `verify_ssl` (false: the console
    /// serves a self-signed certificate on its LAN address; the default), `dial_enabled`,
    /// `gain` (1..16, default 1). A failed validation leaves the previous configuration.
    fn configure(&mut self, cfg: &Map<String, Value>) -> Result<(), DriverError> {
        let mut c = TalkConfig {
            device_id: str_of(cfg, "device_id"),
            host: str_of(cfg, "protect_host"),
            key: str_of(cfg, "protect_api_key"),
            camera_id: str_of(cfg, "camera_id"),
            downlink_url: str_of(cfg, "downlink_url"),
            guard_url: str_of(cfg, "guard_url"),
            verify_ssl: false,
            gain: 1,
            dial_enabled: true,
            scheme: self.scheme.clone(),
            ffmpeg: self.ffmpeg.clone(),
        };
        let err = |m: &str| Err(DriverError::new(format!("unifi_protect_talk: {m}")));
        if c.device_id.is_empty() || c.host.is_empty() || c.key.is_empty() || c.camera_id.is_empty()
        {
            return err("device_id, protect_host, protect_api_key and camera_id are required");
        }
        if c.host.contains(['/', '@', ' ']) {
            return err("protect_host must be a bare host or host:port");
        }
        // Rust-only (PARITY.md 119, 127): the host and the key are written into request
        // headers, the camera id into a request path
        if c.host.bytes().any(|b| b < 0x21 || b == 0x7f) || c.host.contains(['?', '#', '\\']) {
            return err(
                "protect_host must be a bare host or host:port without control characters, spaces or ?#\\",
            );
        }
        if c.key.bytes().any(|b| b < 0x20 || b == 0x7f) {
            return err("protect_api_key must not contain control characters");
        }
        for (name, val, schemes) in [
            (
                "downlink_url",
                c.downlink_url.as_str(),
                &["rtsp", "rtsps"][..],
            ),
            ("guard_url", c.guard_url.as_str(), &["http", "https"][..]),
        ] {
            if val.is_empty() {
                continue;
            }
            let ok = match gourl::parse(val) {
                Ok(u) => !u.hostname().is_empty() && schemes.contains(&u.scheme.as_str()),
                Err(_) => false,
            };
            // Rust-only (PARITY.md 127): both URLs reach a process argument or a request line
            if !ok || !arg_safe(val) {
                return err(&format!("{name} must be a {}:// URL", schemes.join("/")));
            }
        }
        if let Some(v) = cfg.get("verify_ssl") {
            match v {
                Value::Bool(b) => c.verify_ssl = *b,
                _ => return err("verify_ssl must be true or false"),
            }
        }
        if let Some(v) = cfg.get("dial_enabled") {
            match v {
                Value::Bool(b) => c.dial_enabled = *b,
                _ => return err("dial_enabled must be true or false"),
            }
        }
        if let Some(v) = cfg.get("gain") {
            let g = v.as_f64();
            match g {
                Some(g) if g.fract() == 0.0 && (1.0..=16.0).contains(&g) => c.gain = g as i64,
                _ => return err("gain must be a whole number from 1 to 16"),
            }
        }
        crate::panic_hook::register_secret(&c.key);
        let open = self.opener.clone().unwrap_or_else(default_opener);
        // R2-102: a held call (and its two `ffmpeg`s) belongs to the old core, which `stop`
        // could never find again once it is replaced; the hub builds a fresh driver per
        // `configure`, so refuse rather than orphan it
        if lock(&self.core.st).call.is_some() {
            return err("cannot configure a driver that is in a call; stop it first");
        }
        self.core = Arc::new(TalkCore::new(c, self.timing, open));
        Ok(())
    }

    /// Stated here, never in Python: the phone's microphone is pushed to the door
    /// (`TalkbackPush`), the visitor's voice rides the downlink the driver pulls itself
    /// (`InVideoStream`), no relay, no direct mode, and the driver reports availability.
    fn capabilities(&self) -> Capabilities {
        Capabilities {
            inbound_audio: AudioTopology::IN_VIDEO_STREAM,
            outbound_audio: AudioTopology::TALKBACK_PUSH,
            direct_capable: false,
            has_relay: false,
            rtsp_template: String::new(),
            reports_availability: true,
            // the phone may call the door at idle
            can_dial: self.core.cfg.dial_enabled,
            ..Capabilities::default()
        }
    }

    /// The hub keys the driver by this (`registryID`) and `external_ring` names it.
    fn device_id(&self) -> Option<String> {
        Some(self.core.cfg.device_id.clone())
    }

    /// Records the run context and sink. It touches no network: the driver has no watcher, so
    /// "available" means "running"; every answer attempt checks the door itself and fails
    /// closed.
    fn start<'a>(
        &'a self,
        ctx: &'a CancelToken,
        events: Arc<dyn EventSink>,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move {
            {
                let mut st = lock(&self.core.st);
                st.run = Some(ctx.clone());
                st.events = Some(events.clone());
                st.stopped = false;
            }
            events.availability_changed(&self.core.cfg.device_id, true, "");
            Ok(())
        })
    }

    /// Ends the held call (if any) and closes its media. No event is reported.
    fn stop(&self) -> BoxFuture<'_, Result<(), DriverError>> {
        Box::pin(async move {
            let call = {
                // `emit` first, as every reporter takes it: a report in flight finishes before
                // `stopped` is set
                let _emit = lock(&self.core.emit);
                let mut st = lock(&self.core.st);
                st.stopped = true;
                st.call.take()
            };
            let Some(c) = call else { return Ok(()) };
            c.stop();
            if let Some(m) = c.media {
                self.core.close_and_wait(&m).await;
            }
            Ok(())
        })
    }

    /// `Dial` (the legacy, id-less form), `Answer` and `OpenRelay`: there is no SIP dialog and no
    /// relay (`HasRelay` is false; nothing here opens a door). A call out is `dial_call`.
    fn dial<'a>(
        &'a self,
        _ctx: &'a CancelToken,
        _target: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async {
            Err(DriverError::wrapping(
                Sentinel::NotSupported,
                "unifi_protect_talk: Dial: not_supported",
            ))
        })
    }

    fn answer<'a>(
        &'a self,
        _ctx: &'a CancelToken,
        _target: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async {
            Err(DriverError::wrapping(
                Sentinel::NotSupported,
                "unifi_protect_talk: Answer: not_supported (media is bridged in-process)",
            ))
        })
    }

    fn open_relay<'a>(&'a self, _ctx: &'a CancelToken) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async {
            Err(DriverError::wrapping(
                Sentinel::NotSupported,
                "unifi_protect_talk: OpenRelay: not_supported",
            ))
        })
    }

    fn as_audio_claimer(&self) -> Option<&dyn AudioClaimer> {
        Some(self)
    }
    fn as_call_hangupper(&self) -> Option<&dyn CallHangupper> {
        Some(self)
    }
    fn as_reasoned_hangupper(&self) -> Option<&dyn ReasonedHangupper> {
        Some(self)
    }
    fn as_call_dialer(&self) -> Option<&dyn CallDialer> {
        Some(self)
    }
    fn as_connect_answerer(&self) -> Option<&dyn ConnectAnswerer> {
        Some(self)
    }
    fn as_external_ringer(&self) -> Option<&dyn ExternalRinger> {
        Some(self)
    }
    fn as_talk_healther(&self) -> Option<&dyn TalkHealther> {
        Some(self)
    }
}

impl ExternalRinger for TalkDriver {
    fn external_ring(&self, call_id: &str, hold: Duration) -> Result<(), DriverError> {
        self.core.external_ring(call_id, hold)
    }
}

impl CallHangupper for TalkDriver {
    /// `HangupCall`: ends `call_id`, ringing or answered, and closes its media first.
    fn hangup_call(&self, call_id: &str) -> Result<(), DriverError> {
        self.core.hangup_call_why(call_id, "")
    }
}

impl ReasonedHangupper for TalkDriver {
    fn hangup_call_why(&self, call_id: &str, why: &str) -> Result<(), DriverError> {
        self.core.hangup_call_why(call_id, why)
    }
}

impl ConnectAnswerer for TalkDriver {
    fn ringing_call(&self, call_id: &str) -> bool {
        self.core.ringing_call(call_id)
    }

    fn answer_with_audio<'a>(
        &'a self,
        ctx: &'a CancelToken,
        call_id: &'a str,
    ) -> BoxFuture<'a, Result<Arc<dyn CallAudio>, DriverError>> {
        Box::pin(async move { self.core.answer_with_audio(ctx, call_id).await })
    }
}

impl TalkHealther for TalkDriver {
    fn health(&self) -> TalkHealth {
        self.core.health()
    }
}
