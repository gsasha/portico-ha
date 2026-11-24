//! Package `gatesipserver` (E1 of DEV-518's exit ramp, design doc §4.2a): Portico's OWN
//! SIP-server mode, "topology A", the registrar/answerer the Dahua station registers to.
//!
//! ⛔ INERT UNTIL A DEVICE IS CONFIGURED. Importing this module starts nothing; only a Python
//! config entry of this driver sends `configure`. `configure` refuses anything but a loopback
//! bind unless `allow_non_loopback` is set, which then also demands an `allow_from` list.
//! The package has no HTTP client and never touches Home Assistant (the house rings from the
//! button-press automation; doing both would ring twice).
//!
//! # What is here
//!
//! `config.rs`, `media.rs`, `audio.rs`, `netaddr.rs`, `server/` (the `Server`: `mod.rs` holds the
//! state and the loops; `uas.rs` the SIP UAS, `registrar.rs` REGISTER and the CanDial republish
//! bookkeeping, `rtp.rs` the RTP loops, `events.rs` the event queue, `client.rs` the audio client,
//! `dial.rs` the call WE place to the station) and this file's driver glue (`driver.go`, the
//! driver half of `audio.go`).

use crate::util::lock;
use std::sync::{Arc, Mutex};

use serde_json::{Map, Value};

use super::{
    AudioClaimer, AudioTopology, BoxFuture, CallAudio, CallDialer, CallHangupper, Capabilities,
    ConnectAnswerer, Driver, DriverError, EventSink, MediaTarget, Sentinel,
};
use crate::cancel::CancelToken;

pub mod audio;
pub mod config;
pub mod media;
pub mod netaddr;
pub mod server;

pub use audio::{AudioClient, CallAudioImpl};

/// The `Features` entry that says this wheel honours `call_stats` (config) and `stats` (media_open).
pub const FEATURE_CALL_STATS: &str = "call_stats";
pub use config::{parse_config, Config, KEY};
#[cfg(feature = "test-seams")]
pub use server::DialTimers;
pub use server::{DialError, Server, Stats};

/// Go's package-level errors of `server.go` and the refusals of `dial.go` (`ErrNotRegistered` and
/// the rest, which `DialCall` wraps with `%w`). The text of `NoCall` is shown to a person.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ServerError {
    /// `ErrNoCall`
    NoCall,
    /// `ErrAlreadyAnswered` (the package's own, NOT `driver.ErrAlreadyAnswered`)
    AlreadyAnswered,
    /// `ErrAlreadyAttached`
    AlreadyAttached,
    /// `ErrNotRegistered`
    NotRegistered,
    /// `ErrRegistrationStale`
    RegistrationStale,
    /// `ErrDialBusy` itself
    DialBusy,
    /// `fmt.Errorf("%w: another call is in progress", ErrDialBusy)`: what `dial` returns while
    /// the call slot is taken (`is_dial_busy` is true for both)
    DialBusyOtherCall,
    /// `ErrNoAnswer`
    NoAnswer,
    /// `ErrDialCancelled`
    DialCancelled,
    /// `ErrStationRefused`
    StationRefused,
    /// `ErrServerStopped`
    ServerStopped,
    /// `errors.New("no call id")`
    NoCallId,
}

impl ServerError {
    /// `errors.Is(err, ErrDialBusy)`.
    pub fn is_dial_busy(self) -> bool {
        matches!(self, ServerError::DialBusy | ServerError::DialBusyOtherCall)
    }
}

impl std::fmt::Display for ServerError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            ServerError::NoCall => "no call to answer",
            ServerError::AlreadyAnswered => "already answered",
            ServerError::AlreadyAttached => "this call was already answered on another device",
            ServerError::NotRegistered => "the station has not registered",
            ServerError::RegistrationStale => "the station's registration has expired",
            ServerError::DialBusy => "the gate is busy",
            ServerError::DialBusyOtherCall => "the gate is busy: another call is in progress",
            ServerError::NoAnswer => "the station did not answer",
            ServerError::DialCancelled => "the dial was cancelled",
            ServerError::StationRefused => "the station refused the call",
            ServerError::ServerStopped => "the server is stopping",
            ServerError::NoCallId => "no call id",
        })
    }
}

impl std::error::Error for ServerError {}

/// The methods of Go's `*Server` that the driver glue calls. [`Server`] implements it; the
/// tests also have a double.
pub trait ServerApi: Send + Sync {
    /// `ringingCall`: `call_id` is the live inbound call and nobody has answered it.
    fn ringing_call(&self, call_id: &str) -> bool;
    /// `attachFor(call_id)`: attaching IS answering (the 200 OK goes out now). Errors:
    /// `NoCall` (another or finished call, or one we are still placing), `AlreadyAnswered`,
    /// `AlreadyAttached`.
    fn attach_for(&self, call_id: &str) -> Result<Arc<AudioClient>, ServerError>;
    /// `hangupAnswered`: BYE the live call, only if it is `call_id` and was answered.
    fn hangup_answered(&self, call_id: &str) -> bool;
    /// `AnswerCurrent`: the legacy answer of the ringing call. `NoCall`, `AlreadyAnswered`.
    fn answer_current(&self) -> Result<(), ServerError>;
    /// `canDial`: `dial_enabled` AND the station registered recently enough to be called.
    fn can_dial(&self) -> bool;
    /// DEV-518: (the station holds a live registration, unix seconds of its last good REGISTER).
    /// A test double that does not care keeps the default.
    fn station(&self) -> (bool, u64) {
        (false, 0)
    }
    /// `Snapshot`: the counters and the call state.
    fn snapshot(&self) -> Stats;
    /// `dial(ctx, callID)` (dial.go:184): places the call to the station and returns when it has
    /// answered, with the audio client already attached.
    fn dial<'a>(
        &'a self,
        ctx: &'a CancelToken,
        call_id: &'a str,
    ) -> BoxFuture<'a, Result<Arc<AudioClient>, DialError>>;
    /// `shutdown`: BYE an answered call, 480 a ringing one, stop every task, free the ports.
    fn shutdown(&self) -> BoxFuture<'_, ()>;
}

#[derive(Default)]
struct Inner {
    srv: Option<Arc<dyn ServerApi>>,
    /// the same server, when it is the real one (not a test double)
    real: Option<Arc<Server>>,
    /// the audio handed out by `answer_with_audio`
    audio: Option<Arc<CallAudioImpl>>,
}

/// Go's `Driver`.
#[derive(Default)]
pub struct GateSipServer {
    cfg: Config,
    inner: Arc<Mutex<Inner>>,
    /// serialises `start` end to end (Go holds `d.mu` across the bind): a second concurrent
    /// Start waits and then sees `already started`
    start_lock: tokio::sync::Mutex<()>,
}

impl Drop for GateSipServer {
    /// A driver dropped while it still owns a running server (nobody called `stop`) must not
    /// leave its two sockets and five tasks behind. Best effort: needs a runtime.
    fn drop(&mut self) {
        let srv = lock(&self.inner).real.take();
        if let (Some(s), Ok(rt)) = (srv, tokio::runtime::Handle::try_current()) {
            rt.spawn(async move { s.shutdown().await });
        }
    }
}

/// `New`.
pub fn new() -> Box<dyn Driver> {
    Box::new(GateSipServer::default())
}

impl GateSipServer {
    /// `Server()`: the running server (`None` before Start / after Stop).
    pub fn server(&self) -> Option<Arc<dyn ServerApi>> {
        lock(&self.inner).srv.clone()
    }

    /// The validated configuration (Go tests read `d.cfg`).
    pub fn config(&self) -> &Config {
        &self.cfg
    }

    /// The running server as the concrete [`Server`] (Go's tests call `d.Server()` for
    /// `SIPAddr`, `Attach`, `Hangup`); `None` for a test double.
    pub fn real_server(&self) -> Option<Arc<Server>> {
        lock(&self.inner).real.clone()
    }

    /// Installs a server double.
    #[cfg(feature = "test-seams")]
    pub fn set_server(&self, srv: Arc<dyn ServerApi>) {
        let mut g = lock(&self.inner);
        g.srv = Some(srv);
        g.real = None;
    }

    /// Test seam: the audio `answer_with_audio` handed out, as the concrete type (Go's tests
    /// type-assert `*callAudio` to reach `attached`).
    #[cfg(feature = "test-seams")]
    pub fn held_audio(&self) -> Option<Arc<CallAudioImpl>> {
        lock(&self.inner).audio.clone()
    }

    /// Test seam: Go's tests write `d.cfg.ringLimit` / `orphanGrace` between Configure and Start
    /// (`Configure` takes whole seconds, the tests need fractions).
    #[cfg(feature = "test-seams")]
    pub fn set_timers(
        &mut self,
        ring_limit: std::time::Duration,
        orphan_grace: std::time::Duration,
    ) {
        self.cfg.ring_limit = ring_limit;
        self.cfg.orphan_grace = orphan_grace;
    }

    /// Installs the audio `answer_with_audio` would have handed out (tests only).
    #[cfg(feature = "test-seams")]
    pub fn set_audio(&self, a: Arc<CallAudioImpl>) {
        lock(&self.inner).audio = Some(a);
    }

    /// `capabilitiesFor`: the capability set with CanDial given. CanDial is omitted from the
    /// wire when false, so a driver with `dial_enabled` off, or not yet registered, replies
    /// byte for byte as before W4.
    pub fn capabilities_for(cfg: &Config, can_dial: bool) -> Capabilities {
        Capabilities {
            inbound_audio: AudioTopology::SIP_RTP,
            outbound_audio: AudioTopology::SIP_RTP,
            // Media goes through this process's own RTP socket and in-process AudioClient,
            // never straight to a client; no relay is wired in E1, so the lock entity must
            // not offer one.
            direct_capable: false,
            has_relay: false,
            can_dial: can_dial && cfg.dial_enabled,
            ..Capabilities::default()
        }
    }

    /// `capabilities_for` plus the DEV-518 registration fields. `StationRegistered` is always
    /// on the wire for this driver, so a reader can tell a wheel that reports it from one that
    /// does not.
    pub fn capabilities_for_station(
        cfg: &Config,
        can_dial: bool,
        registered: bool,
        last_register_at: u64,
    ) -> Capabilities {
        Capabilities {
            station_registered: Some(registered),
            last_register_at,
            // DEV-811 P3: the list a reader tests instead of a wheel version (a revert wheel is older
            // source under a higher version). `capabilities_for` stays the Go-parity shape.
            features: vec![FEATURE_CALL_STATS],
            ..Self::capabilities_for(cfg, can_dial)
        }
    }

    /// `d.canDial()`: `dial_enabled` in the configuration AND a server that says so.
    fn can_dial(&self) -> bool {
        if !self.cfg.dial_enabled {
            return false;
        }
        self.server().is_some_and(|s| s.can_dial())
    }
}

impl Driver for GateSipServer {
    /// Validates the config; touches no network (Start does). A failed validation leaves the
    /// previous configuration in place.
    ///
    /// Keys: `sip_listen` (required), `password` (required, 8-15 chars), `user` (8001),
    /// `realm` (VDP), `rtp_port` (40000; 0 = kernel-assigned), `ring_seconds` (45),
    /// `orphan_seconds` (15), `allow_from` (IPs/CIDRs), `allow_non_loopback` (false),
    /// `device_id`, `dial_enabled` (false; W4: Talk to the gate).
    fn configure(&mut self, cfg: &Map<String, Value>) -> Result<(), DriverError> {
        self.cfg = parse_config(cfg).map_err(DriverError::new)?;
        Ok(())
    }

    /// The id this driver reports rings under and the key the hub stores it by: the
    /// configured `device_id`, else `gate-sip-server:<sip_listen>`. Empty before Configure.
    fn device_id(&self) -> Option<String> {
        Some(self.cfg.device_id.clone())
    }

    #[cfg(feature = "test-seams")]
    fn sip_addr(&self) -> Option<std::net::SocketAddr> {
        self.real_server().map(|s| s.sip_addr())
    }

    fn capabilities(&self) -> Capabilities {
        let (registered, last) = self.server().map_or((false, 0), |s| s.station());
        Self::capabilities_for_station(&self.cfg, self.can_dial(), registered, last)
    }

    /// Binds the SIP and RTP sockets and starts the server (`newServer(cfg, events).start()`).
    /// Atomic against a second `start`: the one that loses sees `already started`. When `ctx` is
    /// cancelled the server is stopped (Go's watcher goroutine calls `Stop`).
    fn start<'a>(
        &'a self,
        ctx: &'a CancelToken,
        events: Arc<dyn EventSink>,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move {
            let _starting = self.start_lock.lock().await;
            if lock(&self.inner).srv.is_some() {
                return Err(DriverError::new(format!("{KEY}: already started")));
            }
            let srv = Server::start(self.cfg.clone(), events)
                .await
                .map_err(DriverError::new)?;
            {
                let mut g = lock(&self.inner);
                g.srv = Some(srv.clone());
                g.real = Some(srv.clone());
            }
            // The ctx.Done() watcher. Go calls `d.Stop()`, which stops whichever server is
            // current when the context ends; this one stops only the server it was started with
            // (a later Start must not be killed by an earlier context), and ends with it so
            // nothing is left waiting on a context that never ends.
            let (ctx, inner, mine) = (ctx.clone(), self.inner.clone(), srv);
            tokio::spawn(async move {
                tokio::select! {
                    _ = ctx.cancelled() => {}
                    _ = mine.stopped() => return,
                }
                // Shut it down first and forget it after: a `stop` that runs meanwhile finds the
                // server still installed and waits on the same shutdown.
                mine.shutdown().await;
                let mut g = lock(&inner);
                if g.real.as_ref().is_some_and(|r| Arc::ptr_eq(r, &mine)) {
                    g.audio = None;
                    g.srv = None;
                    g.real = None;
                }
            });
            Ok(())
        })
    }

    fn stop(&self) -> BoxFuture<'_, Result<(), DriverError>> {
        Box::pin(async move {
            let srv = {
                let mut g = lock(&self.inner);
                g.audio = None;
                g.real = None;
                g.srv.take()
            };
            if let Some(s) = srv {
                s.shutdown().await;
            }
            Ok(())
        })
    }

    /// Placing a call to the station (gate-sip's /call, the app's "talk to the gate") is not
    /// built in E1.
    fn dial<'a>(
        &'a self,
        _ctx: &'a CancelToken,
        _target: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move {
            Err(DriverError::wrapping(
                Sentinel::NotSupported,
                format!("{KEY}: Dial: not_supported (outbound calls are not part of E1)"),
            ))
        })
    }

    /// Answers the ringing call with this process's own RTP socket (audio is then reached
    /// through the server's `Attach`). A client-supplied MediaTarget would mean "send the
    /// station's RTP straight to the client", which this mode does not do: it is refused
    /// rather than ignored.
    fn answer<'a>(
        &'a self,
        _ctx: &'a CancelToken,
        target: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move {
            if target != MediaTarget::default() {
                return Err(DriverError::wrapping(
                    Sentinel::NotSupported,
                    format!(
                        "{KEY}: Answer with a media target: not_supported (media is bridged in-process)"
                    ),
                ));
            }
            let Some(s) = self.server() else {
                return Err(DriverError::new(format!("{KEY}: not started")));
            };
            s.answer_current()
                .map_err(|e| DriverError::new(e.to_string()))
        })
    }

    fn open_relay<'a>(&'a self, _ctx: &'a CancelToken) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move {
            Err(DriverError::wrapping(
                Sentinel::NotSupported,
                format!("{KEY}: OpenRelay: not_supported"),
            ))
        })
    }

    fn as_audio_claimer(&self) -> Option<&dyn AudioClaimer> {
        Some(self)
    }
    fn as_call_hangupper(&self) -> Option<&dyn CallHangupper> {
        Some(self)
    }
    fn as_connect_answerer(&self) -> Option<&dyn ConnectAnswerer> {
        Some(self)
    }
    fn as_call_dialer(&self) -> Option<&dyn CallDialer> {
        Some(self)
    }
}

impl AudioClaimer for GateSipServer {
    /// The audio of `call_id` if it exists, is not over, and no client leg has taken it yet.
    /// After `answer_with_audio` it is `None`: the leg that answered holds it, and a second
    /// joiner must not be given the same pair.
    fn claimed_audio(&self, call_id: &str) -> Option<Arc<dyn CallAudio>> {
        let a = lock(&self.inner).audio.clone()?;
        if a.call_id() != call_id || a.over() || a.attached() {
            return None;
        }
        Some(a)
    }
}

impl CallHangupper for GateSipServer {
    /// BYEs a call this driver answered (`NoSuchCall` for any other: a ringing call, a
    /// finished one, an unknown id).
    fn hangup_call(&self, call_id: &str) -> Result<(), DriverError> {
        match self.server() {
            Some(s) if s.hangup_answered(call_id) => Ok(()),
            _ => Err(DriverError::sentinel(Sentinel::NoSuchCall)),
        }
    }
}

impl ConnectAnswerer for GateSipServer {
    /// Whether `call_id` is the live call and nobody has answered it.
    fn ringing_call(&self, call_id: &str) -> bool {
        self.server().is_some_and(|s| s.ringing_call(call_id))
    }

    /// Answers the ringing call `call_id` (200 OK goes out now) and returns its audio, taken
    /// by the caller. `NoSuchCall` for any other call, `AlreadyAnswered` when somebody
    /// already has it (a second answerer is refused, the first keeps the call).
    fn answer_with_audio<'a>(
        &'a self,
        _ctx: &'a CancelToken,
        call_id: &'a str,
    ) -> BoxFuture<'a, Result<Arc<dyn CallAudio>, DriverError>> {
        Box::pin(async move {
            let Some(s) = self.server() else {
                return Err(DriverError::wrapping(
                    Sentinel::NoSuchCall,
                    format!("{KEY}: not started: no_such_call"),
                ));
            };
            let cl = match s.attach_for(call_id) {
                Ok(cl) => cl,
                Err(ServerError::NoCall) => {
                    return Err(DriverError::wrapping(
                        Sentinel::NoSuchCall,
                        format!("{KEY}: {call_id} is not a live call: no_such_call"),
                    ))
                }
                Err(ServerError::AlreadyAnswered | ServerError::AlreadyAttached) => {
                    return Err(DriverError::wrapping(
                        Sentinel::AlreadyAnswered,
                        format!("{KEY}: {call_id}: already_answered"),
                    ))
                }
                Err(e) => return Err(DriverError::new(e.to_string())),
            };
            let a = CallAudioImpl::new(call_id, cl);
            a.mark_attached(); // the caller has it: claimed_audio will not hand it out again
            let mut g = lock(&self.inner);
            if g.srv.as_ref().is_some_and(|cur| Arc::ptr_eq(cur, &s)) {
                g.audio = Some(a.clone());
            }
            Ok(a as Arc<dyn CallAudio>)
        })
    }
}

impl CallDialer for GateSipServer {
    /// `DialCall`: places the call to the gate and returns once the station has answered, or
    /// with the reason it did not. With `dial_enabled` off it returns exactly what the legacy
    /// Dial does (`NotSupported`), without any network traffic.
    fn dial_call<'a>(
        &'a self,
        ctx: &'a CancelToken,
        call_id: &'a str,
        _target: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move {
            if !self.cfg.dial_enabled {
                return self.dial(ctx, MediaTarget::default()).await;
            }
            let Some(s) = self.server() else {
                return Err(DriverError::new(format!("{KEY}: DialCall: not started")));
            };
            let cl = match s.dial(ctx, call_id).await {
                Ok(cl) => cl,
                Err(e) => return Err(DriverError::new(format!("{KEY}: DialCall: {e}"))),
            };
            let a = CallAudioImpl::new(call_id, cl);
            let mut g = lock(&self.inner);
            if g.srv.as_ref().is_some_and(|cur| Arc::ptr_eq(cur, &s)) {
                g.audio = Some(a);
            }
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests;
