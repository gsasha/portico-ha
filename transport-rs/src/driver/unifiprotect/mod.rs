//! Package `unifiprotect` (`transport/pkg/driver/unifiprotect/`, DEV-764): the `unifi_protect`
//! driver (design doc §4.2, §9.3) and the `unifi_protect_talk` driver.
//!
//! Not SIP or ONVIF at all: Protect's own Integration API. Capabilities: `InboundAudio =
//! InVideoStream` (rides the RTSP feed every client already pulls for video), `OutboundAudio =
//! TalkbackPush` (a REST-opened RTP push session), `DirectCapable = false`.
//!
//! The core is `unifiprotect.go`, `api.go`, `ws.go` and `ring.go`: configure, the console API
//! client ([`api`]: the RTSPS stream URL and the talkback session), the events websocket
//! ([`ws`], over [`tls`]), ring detection and its call state ([`ring`]), availability, `Stop`.
//! The talkback itself is [`talkback`]: an `ffmpeg` transcode into the REST-opened session
//! (`talkback.go`), installed by default through the [`TalkbackOpener`] seam, so `OpenRelay` and
//! `AnswerTalkback` open the real session and the hub's accessor
//! [`Driver::as_talkback_answerer`] is `Some`. The `unifi_protect_talk` driver
//! (`talk*.go`) is [`talk`], [`talk_media`], [`talk_down`] and [`talk_dial`], with [`ffmpeg`] for
//! the command lines. A driver whose opener was removed (a test) answers with the verb's own failure code
//! BEFORE it touches the console.
//!
//! # Differences from Go
//!
//! docs/parity/unifi_protect.md, "unifi_protect core": differences 107 (the timings and the URL
//! scheme are per-driver fields instead of package variables), 108 (the HTTP client), 109 (TLS),
//! 111 (the reader is one task that selects over the socket instead of a reader plus a ping
//! goroutine), 113 (the supervised watcher: a panic is contained, reported unavailable and the
//! watcher restarted), 115 (`Stop` waits for the watcher at most 10 s), 116 (a dropped
//! `AnswerTalkback` future gives the ring back), 119 (stricter configuration) and 121 (`Stop`
//! orders itself against a report in flight).

pub mod api;
pub mod ffmpeg;
pub mod ring;
pub mod talk;
pub mod talk_dial;
pub mod talk_down;
pub mod talk_media;
pub mod talkback;
// R3B-8: the TLS layer is `gostd::tls` (the observer's and the shared HTTP client's too); the old path stays.
pub use crate::gostd::tls;
pub mod ws;

use crate::seams::PanicFlag;
use crate::util::lock;
use std::sync::atomic::AtomicU64;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{Map, Value};
use tokio::task::JoinHandle;
use tokio::time::Instant;

use super::{
    AudioTopology, BoxFuture, CallHangupper, Capabilities, Driver, DriverError, EventSink,
    MediaTarget, Sentinel, StreamSourcer, TalkbackAnswerer, TalkbackSink,
};
use crate::cancel::CancelToken;
use api::{ProtectApi, TalkbackSession};

pub const KEY: &str = "unifi_protect";
pub const DEFAULT_QUALITY: &str = "high";
/// `eventsPath`.
pub const EVENTS_PATH: &str = "/proxy/protect/integration/v1/subscribe/events";
/// How long `stop` waits for the watcher (Go waits for ever).
pub const STOP_WAIT: Duration = Duration::from_secs(10);
/// How long `stop` waits for a closed talkback to release its subprocess (Go waits for ever).
pub const CLOSE_WAIT: Duration = Duration::from_secs(5);

/// The package variables of `ring.go` that tests shrink. Per driver here, so tests run in
/// parallel and a driver captures its own at `configure`/`start`.
#[derive(Clone, Copy, Debug)]
pub struct Timing {
    /// an `end` before this is honoured only once it has passed
    pub ring_min_hold: Duration,
    /// a ring with no `end` at all is over after this
    pub ring_max_hold: Duration,
    /// keepalive ping; UniFi OS's proxy drops an idle tunnel after 10 minutes
    pub ping_interval: Duration,
    /// nothing at all from the console for this long: reconnect
    pub pong_wait: Duration,
    pub backoff_min: Duration,
    pub backoff_max: Duration,
    /// a connection that lived this long resets the backoff
    pub stable_after: Duration,
    pub dial_timeout: Duration,
    /// how long `stop` waits for the watcher (Go waits for ever)
    pub stop_wait: Duration,
}

impl Default for Timing {
    fn default() -> Self {
        Timing {
            ring_min_hold: Duration::from_secs(10),
            ring_max_hold: Duration::from_secs(60),
            ping_interval: Duration::from_secs(30),
            pong_wait: Duration::from_secs(90),
            backoff_min: Duration::from_secs(1),
            backoff_max: Duration::from_secs(60),
            stable_after: Duration::from_secs(30),
            dial_timeout: Duration::from_secs(10),
            stop_wait: STOP_WAIT,
        }
    }
}

#[derive(Clone, Default)]
pub(crate) struct Config {
    /// `nvr_host`, also the device id a ring is reported under
    pub host_port: String,
    pub device_id: String,
    pub camera_id: String,
    pub key: String,
    pub quality: String,
    /// captured at configure, so a watcher never reads a changing value
    pub scheme: String,
    pub ring_events: bool,
}

/// A send-only talkback session the driver holds (`*Talkback`): its audio sink and a token that
/// is cancelled when the session is over, by `close` or on its own.
pub struct OpenedTalkback {
    pub sink: Arc<dyn TalkbackSink>,
    pub done: CancelToken,
}

/// The seam that starts the talkback (the `ffmpeg` transcode into the REST-opened session).
/// `ctx` is the run context of the driver.
pub trait TalkbackOpener: Send + Sync {
    /// The cheap local checks, run BEFORE the console is asked for a session (PARITY.md 152): a
    /// session armed for a talkback that cannot start only lapses on its own.
    fn preflight(&self) -> BoxFuture<'_, Result<(), DriverError>> {
        Box::pin(async { Ok(()) })
    }

    fn start<'a>(
        &'a self,
        ctx: &'a CancelToken,
        session: TalkbackSession,
    ) -> BoxFuture<'a, Result<OpenedTalkback, DriverError>>;
}

/// `ringCall`: the one call this camera can have at a time.
pub(crate) struct RingCall {
    /// identity for timers and the answer path (Go compares the pointer)
    pub serial: u64,
    pub id: String,
    pub event_id: String,
    pub started: Instant,
    /// an `AnswerTalkback` is opening the session
    pub answering: bool,
    pub answered: bool,
    pub end_timer: Option<JoinHandle<()>>,
    pub max_timer: Option<JoinHandle<()>>,
    pub talkback: Option<Arc<OpenedTalkback>>,
}

impl RingCall {
    /// `stopTimers`: the handles stay set, as Go's timers do (`endTimer != nil` still means "an
    /// end was seen").
    pub fn stop_timers(&self) {
        if let Some(h) = &self.end_timer {
            h.abort();
        }
        if let Some(h) = &self.max_timer {
            h.abort();
        }
    }
}

pub(crate) struct Watch {
    pub cancel: CancelToken,
    pub done: CancelToken,
}

/// What `d.mu` guards.
#[derive(Default)]
pub(crate) struct State {
    pub ctx: Option<CancelToken>,
    pub events: Option<Arc<dyn EventSink>>,
    pub stopped: bool,
    /// bumped by every `start`: an open that began in an earlier run is stale even when `stopped`
    /// was cleared again by a restart (DEV-796 D5)
    pub run: u64,
    pub call: Option<RingCall>,
    pub talkback: Option<Arc<OpenedTalkback>>,
    pub watch: Option<Watch>,
    pub opener: Option<Arc<dyn TalkbackOpener>>,
}

/// The driver's shared core (what the watcher, the timers and the hub-facing methods use).
pub(crate) struct Core {
    pub cfg: Config,
    pub timing: Timing,
    pub api: Mutex<Option<Arc<ProtectApi>>>,
    /// `d.mu`
    pub st: Mutex<State>,
    /// `emitMu`: serialises what is reported to the sink (taken before `st`). Never held across
    /// an `.await`.
    pub emit: Mutex<()>,
    /// `availMu`, `availKnown`, `availLast`
    pub avail: Mutex<(bool, bool)>,
    pub serial: AtomicU64,
    /// test seam: the watcher panics when it starts (once)
    pub panic_in_watch: PanicFlag,
}

/// Go's `Driver`.
pub struct UnifiProtect {
    core: Arc<Core>,
    scheme: String,
    timing: Timing,
    /// Go's package variable `ffmpegBinary`: the name looked up on `PATH` when a session opens
    ffmpeg: String,
    /// a test's opener: kept on the driver, so it survives a later `configure` (which builds a
    /// new core), as `TalkDriver`'s does
    opener: Mutex<Option<Arc<dyn TalkbackOpener>>>,
}

/// `New`.
pub fn new() -> Box<dyn Driver> {
    Box::new(UnifiProtect::default())
}

impl Core {
    fn new(cfg: Config, timing: Timing, api: Option<ProtectApi>) -> Core {
        Core {
            cfg,
            timing,
            api: Mutex::new(api.map(Arc::new)),
            st: Mutex::new(State::default()),
            emit: Mutex::new(()),
            avail: Mutex::new((false, false)),
            serial: AtomicU64::new(0),
            panic_in_watch: PanicFlag::default(),
        }
    }

    pub fn api(&self) -> Option<Arc<ProtectApi>> {
        lock(&self.api).clone()
    }

    pub fn sink(&self) -> Option<Arc<dyn EventSink>> {
        lock(&self.st).events.clone()
    }
}

impl Default for UnifiProtect {
    fn default() -> Self {
        let timing = Timing::default();
        let core = Arc::new(Core::new(Config::default(), timing, None));
        lock(&core.st).opener = Some(Arc::new(talkback::FfmpegOpener::new(
            ffmpeg::FFMPEG_BINARY,
            "",
        )));
        UnifiProtect {
            core,
            scheme: "https".to_string(),
            timing,
            ffmpeg: ffmpeg::FFMPEG_BINARY.to_string(),
            opener: Mutex::new(None),
        }
    }
}

impl UnifiProtect {
    /// `scheme` (a package variable in Go "only so a test can point the driver at a plain-HTTP
    /// fake console"): per driver, set before `configure`, which captures it.
    #[cfg(feature = "test-seams")]
    pub fn set_scheme(&mut self, scheme: &str) {
        self.scheme = scheme.to_string();
    }

    /// The ring timers and backoffs (Go: `ringMinHold`, ... package variables), set before
    /// `configure`.
    #[cfg(feature = "test-seams")]
    pub fn set_timing(&mut self, t: Timing) {
        self.timing = t;
    }

    /// `d.api = newProtectAPI(...)`, as a test replaces it.
    #[cfg(feature = "test-seams")]
    pub fn set_api_for_test(&self, api: ProtectApi) {
        *lock(&self.core.api) = Some(Arc::new(api));
    }

    /// Go's `ffmpegBinary` (a package variable "only so a test can point the driver at a fake
    /// ffmpeg script"): per driver, set before `configure`, which installs the opener for it.
    #[cfg(feature = "test-seams")]
    pub fn set_ffmpeg_binary(&mut self, path: &str) {
        self.ffmpeg = path.to_string();
        lock(&self.core.st).opener = Some(Arc::new(talkback::FfmpegOpener::new(
            path,
            &self.core.cfg.key,
        )));
    }

    /// The seam, see [`TalkbackOpener`]: replaces the `ffmpeg` opener (tests), now and after a
    /// later `configure`.
    pub fn set_talkback_opener(&self, o: Arc<dyn TalkbackOpener>) {
        *lock(&self.opener) = Some(o.clone());
        lock(&self.core.st).opener = Some(o);
    }

    /// No opener at all (tests of the refusal that needs none).
    #[cfg(feature = "test-seams")]
    pub fn clear_talkback_opener(&self) {
        *lock(&self.opener) = None;
        lock(&self.core.st).opener = None;
    }

    /// Test seam: the watcher task panics as soon as it runs (once).
    #[cfg(feature = "test-seams")]
    pub fn panic_in_watch_for_test(&self) {
        self.core.panic_in_watch.arm();
    }

    /// Is a talkback session held (`TalkbackAudio` reports it in Go)?
    pub fn has_talkback(&self) -> bool {
        lock(&self.core.st).talkback.is_some()
    }

    /// `TalkbackAudio`: where client-leg frames go while a talkback session is open.
    pub fn talkback_audio(&self) -> Option<tokio::sync::mpsc::Sender<Vec<u8>>> {
        lock(&self.core.st)
            .talkback
            .as_ref()
            .map(|t| t.sink.input())
    }

    /// The serial of the live ring call, if any (tests).
    #[cfg(feature = "test-seams")]
    pub fn ring_serial_for_test(&self) -> Option<u64> {
        lock(&self.core.st).call.as_ref().map(|c| c.serial)
    }

    /// `endRinging` for a given call serial (tests): a stale serial must do nothing.
    #[cfg(feature = "test-seams")]
    pub fn end_ringing_for_test(&self, serial: u64) {
        self.core.end_ringing(serial);
    }

    /// `handle`, exposed for the vectors and the unit tests: one events frame.
    #[cfg(feature = "test-seams")]
    pub fn handle_frame_for_test(&self, raw: &[u8]) {
        self.core.handle(raw);
    }
}

impl Driver for UnifiProtect {
    /// Validates the config-flow fields (`drivers/unifi_protect.py`) and builds the API client;
    /// it touches no network. `verify_ssl` is optional and defaults to true; a console with its
    /// default self-signed certificate needs it set false. A failed validation leaves the
    /// previous configuration in place.
    fn configure(&mut self, cfg: &Map<String, Value>) -> Result<(), DriverError> {
        let get = |k: &str| match cfg.get(k) {
            Some(Value::String(s)) => s.clone(),
            _ => String::new(),
        };
        let (host, key, cam) = (get("nvr_host"), get("nvr_api_key"), get("camera_id"));
        if host.is_empty() || key.is_empty() || cam.is_empty() {
            return Err(DriverError::new(
                "unifi_protect: nvr_host, nvr_api_key and camera_id are required",
            ));
        }
        if host.contains(['/', '@', ' ']) {
            return Err(DriverError::new(
                "unifi_protect: nvr_host must be a bare host or host:port",
            ));
        }
        // Rust-only (PARITY.md 119): the host and the key are written into request headers
        if host.bytes().any(|b| b < 0x21 || b == 0x7f) || host.contains(['?', '#', '\\']) {
            return Err(DriverError::new(
                "unifi_protect: nvr_host must be a bare host or host:port without control characters, spaces or ?#\\",
            ));
        }
        if key.bytes().any(|b| b < 0x20 || b == 0x7f) {
            return Err(DriverError::new(
                "unifi_protect: nvr_api_key must not contain control characters",
            ));
        }
        let verify = match cfg.get("verify_ssl") {
            Some(Value::Bool(b)) => *b,
            _ => true,
        };
        let quality = match get("stream_quality") {
            q if q.is_empty() => DEFAULT_QUALITY.to_string(),
            q => q,
        };
        let ring = match cfg.get("ring_events") {
            None => false,
            Some(Value::Bool(b)) => *b,
            Some(_) => {
                return Err(DriverError::new(
                    "unifi_protect: ring_events must be true or false",
                ))
            }
        };
        crate::panic_hook::register_secret(&key);
        let api = ProtectApi::new(&self.scheme, &host, &key, verify);
        let c = Config {
            host_port: host.clone(),
            device_id: host,
            camera_id: cam,
            key,
            quality,
            scheme: self.scheme.clone(),
            ring_events: ring,
        };
        // R2-102: a started driver's watcher, ring and talkback belong to the old core, which
        // `stop` could never find again once it is replaced (Go mutates the fields in place).
        // The hub builds a fresh driver per `configure`, so this is a trap, not a path: refuse.
        {
            let st = lock(&self.core.st);
            if st.watch.is_some() || st.call.is_some() || st.talkback.is_some() {
                return Err(DriverError::new(
                    "unifi_protect: cannot configure a driver that is running a watcher or a call; stop it first",
                ));
            }
        }
        self.core = Arc::new(Core::new(c, self.timing, Some(api)));
        let key = self.core.cfg.key.clone();
        let opener = lock(&self.opener).clone().unwrap_or_else(|| {
            Arc::new(talkback::FfmpegOpener::new(&self.ffmpeg, &key)) as Arc<dyn TalkbackOpener>
        });
        lock(&self.core.st).opener = Some(opener);
        Ok(())
    }

    fn capabilities(&self) -> Capabilities {
        Capabilities {
            inbound_audio: AudioTopology::IN_VIDEO_STREAM,
            outbound_audio: AudioTopology::TALKBACK_PUSH,
            direct_capable: false,
            has_relay: true,
            rtsp_template: "rtsps://{nvr_host}/{camera_id}".to_string(),
            // Only a driver that watches the events stream has an availability to report;
            // otherwise the entities follow the transport alone.
            reports_availability: self.core.cfg.ring_events,
            can_dial: false,
            ..Capabilities::default()
        }
    }

    /// Records the run context and, only when the driver was configured with `ring_events:
    /// true`, starts watching the console's public events websocket for the doorbell ring
    /// (`ring.rs`). Without it, `start` touches no network. The first connection is real
    /// traffic to the console: the §7 activation gate applies.
    fn start<'a>(
        &'a self,
        ctx: &'a CancelToken,
        events: Arc<dyn EventSink>,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move {
            let spawn = {
                let mut st = lock(&self.core.st);
                // a watcher that `stop` gave up on is still running (it was told to end): a
                // second one beside it would feed the same core twice (PARITY.md 353)
                if st
                    .watch
                    .as_ref()
                    .is_some_and(|w| w.cancel.is_cancelled() && !w.done.is_cancelled())
                {
                    return Err(DriverError::new(format!(
                        "{KEY}: the previous events watcher has not stopped yet"
                    )));
                }
                st.ctx = Some(ctx.clone());
                st.events = Some(events);
                st.stopped = false;
                st.run += 1;
                if st.watch.as_ref().is_some_and(|w| w.done.is_cancelled()) {
                    st.watch = None; // over for good: a new one may start
                }
                if self.core.cfg.ring_events && st.watch.is_none() {
                    let w = Watch {
                        cancel: CancelToken::new(),
                        done: CancelToken::new(),
                    };
                    let r = (w.cancel.clone(), w.done.clone());
                    st.watch = Some(w);
                    Some(r)
                } else {
                    None
                }
            };
            if let Some((cancel, done)) = spawn {
                ring::spawn_watcher(self.core.clone(), ctx.clone(), cancel, done);
            }
            Ok(())
        })
    }

    /// Ends the ring watcher and any open talkback session. The ring call a stop finds is
    /// dropped without a report (Go: `endLocked` reports nothing once `stopped`).
    fn stop(&self) -> BoxFuture<'_, Result<(), DriverError>> {
        Box::pin(async move {
            let (watch, talkback) = {
                // `emit` first, as every reporter takes it: a report in flight finishes before
                // `stopped` is set (Go sets it without)
                let _emit = lock(&self.core.emit);
                let mut st = lock(&self.core.st);
                st.stopped = true;
                let w = st.watch.take();
                if let Some(c) = st.call.take() {
                    c.stop_timers();
                }
                (w, st.talkback.take())
            };
            if let Some(w) = watch {
                w.cancel.cancel();
                if tokio::time::timeout(self.core.timing.stop_wait, w.done.cancelled())
                    .await
                    .is_err()
                {
                    crate::panic_hook::log_line(&format!(
                        "portico-transport: {KEY}: the events watcher did not stop within {:?}",
                        self.core.timing.stop_wait
                    ));
                    // it is still running: keep it in the slot, so a `start` does not add a
                    // second watcher beside it
                    lock(&self.core.st).watch = Some(w);
                }
            }
            match talkback {
                Some(t) => {
                    let r = t.sink.close();
                    // the process is reaped before `stop` returns (bounded: a sink that never
                    // reports is not waited for)
                    let _ = tokio::time::timeout(CLOSE_WAIT, t.sink.closed()).await;
                    r
                }
                None => Ok(()),
            }
        })
    }

    /// There is no SIP dialog with the device to place or join (`DirectCapable` false); the audio
    /// path is `open_relay`.
    fn dial<'a>(
        &'a self,
        _ctx: &'a CancelToken,
        _target: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move {
            Err(DriverError::wrapping(
                Sentinel::NotSupported,
                "unifi_protect: no SIP dialog to dial (talkback is REST + RTP push): not_supported",
            ))
        })
    }

    fn answer<'a>(
        &'a self,
        _ctx: &'a CancelToken,
        _target: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move {
            Err(DriverError::wrapping(
                Sentinel::NotSupported,
                "unifi_protect: no SIP dialog to answer (talkback is REST + RTP push): not_supported",
            ))
        })
    }

    /// Opens the talkback session on the console and starts the transcode into it. Real
    /// traffic to the console and, via `ffmpeg`, RTP to the camera: the activation-gate rule
    /// (§7) applies.
    fn open_relay<'a>(&'a self, ctx: &'a CancelToken) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move { self.core.open_talkback(ctx).await.map(|_| ()) })
    }

    fn as_call_hangupper(&self) -> Option<&dyn CallHangupper> {
        Some(self)
    }
    fn as_stream_sourcer(&self) -> Option<&dyn StreamSourcer> {
        Some(self)
    }
    /// `Some` while an opener is installed (the `ffmpeg` one, from `configure` on): the hub's
    /// `findTalkbackAnswerer` offers a ring to it. A driver whose opener a test removed is not
    /// offered rings it could not answer.
    fn as_talkback_answerer(&self) -> Option<&dyn TalkbackAnswerer> {
        lock(&self.core.st)
            .opener
            .is_some()
            .then_some(self as &dyn TalkbackAnswerer)
    }
}

impl StreamSourcer for UnifiProtect {
    /// Mints the camera's RTSPS share and returns its URL, which embeds a console-issued token
    /// (a secret; never logged).
    fn stream_url<'a>(
        &'a self,
        ctx: &'a CancelToken,
    ) -> BoxFuture<'a, Result<String, DriverError>> {
        Box::pin(async move {
            let Some(api) = self.core.api() else {
                return Err(DriverError::new("unifi_protect: not configured"));
            };
            api.rtsps_url(ctx, &self.core.cfg.camera_id, &self.core.cfg.quality)
                .await
                .map_err(DriverError::new)
        })
    }
}

impl CallHangupper for UnifiProtect {
    /// Ends `call_id`, ringing or answered, and closes its talkback.
    fn hangup_call(&self, call_id: &str) -> Result<(), DriverError> {
        self.core.hangup_call(call_id)
    }
}

impl TalkbackAnswerer for UnifiProtect {
    fn ringing_call(&self, call_id: &str) -> bool {
        self.core.ringing_call(call_id)
    }

    /// The answer for a Protect ring: a client has landed, so open the REST talkback session
    /// and the transcode and mark the call answered. A failed open leaves the call ringing, so
    /// another client can try.
    fn answer_talkback<'a>(
        &'a self,
        ctx: &'a CancelToken,
        call_id: &'a str,
    ) -> BoxFuture<'a, Result<Arc<dyn TalkbackSink>, DriverError>> {
        Box::pin(async move { self.core.answer_talkback(ctx, call_id).await })
    }
}

impl Core {
    /// `openTalkback`: the REST session and the transcode, stored as THE talkback of the
    /// driver. With no opener (a test removed it) nothing is touched.
    pub(crate) async fn open_talkback(
        self: &Arc<Self>,
        ctx: &CancelToken,
    ) -> Result<Arc<OpenedTalkback>, DriverError> {
        let Some(api) = self.api() else {
            return Err(DriverError::new("unifi_protect: not configured"));
        };
        let (opener, run_ctx, run) = {
            let st = lock(&self.st);
            if st.stopped {
                return Err(DriverError::new("unifi_protect: stopped"));
            }
            if st.talkback.is_some() {
                return Err(DriverError::new(
                    "unifi_protect: a talkback session is already open",
                ));
            }
            (st.opener.clone(), st.ctx.clone(), st.run)
        };
        let Some(opener) = opener else {
            return Err(DriverError::new(
                "unifi_protect: no talkback opener is installed",
            ));
        };
        let run_ctx = run_ctx.unwrap_or_default();
        opener
            .preflight()
            .await
            .map_err(|e| DriverError::new(format!("unifi_protect: {}", e.message())))?;
        let session = api
            .talkback_session(ctx, &self.cfg.camera_id)
            .await
            .map_err(|e| {
                DriverError::new(format!("unifi_protect: opening talkback session: {e}"))
            })?;
        let t = opener
            .start(&run_ctx, session)
            .await
            .map_err(|e| DriverError::new(format!("unifi_protect: {}", e.message())))?;
        let t = Arc::new(t);
        let verdict = {
            let mut st = lock(&self.st);
            if st.stopped || st.run != run {
                // `stop` already took the slot and will not look again (a restart since does not
                // make this open current): a session stored now would be nobody's to close
                // (DEV-796 D5)
                Some(true)
            } else if st.talkback.is_some() {
                // lost a race with a concurrent OpenRelay
                Some(false)
            } else {
                st.talkback = Some(t.clone());
                None
            }
        };
        match verdict {
            Some(true) => {
                let _ = t.sink.close();
                // the process is reaped before returning, as `stop` does (bounded)
                let _ = tokio::time::timeout(CLOSE_WAIT, t.sink.closed()).await;
                return Err(DriverError::new("unifi_protect: stopped while opening"));
            }
            Some(false) => {
                let _ = t.sink.close();
                return Err(DriverError::new(
                    "unifi_protect: a talkback session is already open",
                ));
            }
            None => {}
        }
        // a session that ends on its own frees the slot
        let (core, t2) = (self.clone(), t.clone());
        tokio::spawn(async move {
            t2.done.cancelled().await;
            let mut st = lock(&core.st);
            if st.talkback.as_ref().is_some_and(|x| Arc::ptr_eq(x, &t2)) {
                st.talkback = None;
            }
        });
        Ok(t)
    }
}
