//! Package `dahua` (`transport/pkg/driver/dahua/dahua.go`, DEV-764 slice 6): the `dahua_sip`
//! driver (design doc §4.2, §9.3). It registers the `user#index` account on a Dahua VTO with the
//! shared [`sip::Endpoint`], reports an inbound INVITE as a ring, answers it on request, places a
//! call to the device, and keeps one pending ring for the relay path (`internal/call/relay.go`,
//! `crate::call::relay`): the browser answer takes the ring through [`PendingRinger`] and the
//! 200 OK goes out only once the browser leg connected.
//!
//! The driver is a SIP user agent only. It sends no RTP of its own; `Answer`/`Dial` offer the
//! hub-given media target in the SDP (direct mode) and the relay opens its own audio socket.
//! `OpenRelay` is refused with Go's text (its wire protocol is an open question in Go too).
//!
//! # Differences from Go
//!
//! See docs/parity/dahua.md (slice 6). In short: a driver that was not started refuses `Dial` with an error
//! (Go dereferences a nil endpoint), a second `Start` is refused (Go leaks the first endpoint),
//! and a panic in the registration task is contained and reported (Go's would end the process).

#[cfg(feature = "test-seams")]
use crate::seams::PanicFlag;
use crate::util::lock;
#[cfg(feature = "test-seams")]
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{Map, Value};

use super::{
    AudioTopology, BoxFuture, Capabilities, Driver, DriverError, EventSink, MediaTarget,
    PendingRinger, STATE_ENDED,
};
use crate::cancel::CancelToken;
use crate::panic_hook::{log_line, out_line};
use crate::sip::{self, Call, Endpoint, IncomingRequest, Registrar, SdpInfo};

pub const KEY: &str = "dahua_sip";

/// `stopTimeout`: how long `stop` waits for the clean de-register; an unreachable device must
/// not hang shutdown.
pub const STOP_TIMEOUT: Duration = Duration::from_secs(3);

/// `sipPort`: the device's SIP port (0 = 5060). Go has a package variable "only so a test can
/// point the driver at a fake device on an ephemeral port"; this is the same seam for the tests
/// that reach the driver through the hub (they serialise on it). A driver can also be given its
/// own port ([`Dahua::set_sip_port`]), which wins.
#[cfg(feature = "test-seams")]
pub static SIP_PORT: AtomicI64 = AtomicI64::new(0);

/// A registrar that ran this long before it panicked was healthy: its panic is a first failure,
/// not one more of a streak (the delay would otherwise stay at its cap for good).
pub const RESTART_STABLE: Duration = Duration::from_secs(120);

/// The failures count and the wait before the registrar is started again after a panic: the
/// wait doubles from `base` per failure of a streak, capped at 5 min; a run of `stable_after` or
/// more before the panic starts the streak again.
pub fn restart_plan(
    failures: u32,
    ran: Duration,
    base: Duration,
    stable_after: Duration,
) -> (u32, Duration) {
    let failures = if ran >= stable_after { 1 } else { failures + 1 };
    let wait = base
        .saturating_mul(1 << (failures - 1).min(6))
        .min(Duration::from_secs(300));
    (failures, wait)
}

/// `config`.
#[derive(Clone, Default)]
struct Config {
    host: String,
    sip_user: String,
    sip_password: String,
}

/// What the INVITE/CANCEL/BYE handlers and the hub-facing methods share (Go's `d.mu` and the
/// fields it guards). The lock is never held across an `.await`.
#[derive(Default)]
struct RingState {
    /// set by the INVITE handler, consumed by `answer` / `pending_ring*`
    pending_ring: Option<IncomingRequest>,
    /// Call-ID of the last INVITE rung, to drop its retransmits
    ring_call_id: String,
    /// To-tag generated once at ring time, used in the 180 and the 200
    ring_tag: String,
    /// The answered or dialled call. Go stores it and never reads it (nothing ends a direct
    /// call from IPC); kept so the dialog stays alive and the BYE handler drops it.
    active_call: Option<Arc<Call>>,
}

impl RingState {
    /// `dropPendingRing`: forgets the ringing INVITE if it is `call_id`'s (a BYE ends that call,
    /// so a later Answer/PendingRing must not "answer" a dead INVITE); a BYE for any other call
    /// leaves the current ring alone.
    fn drop_pending_ring(&mut self, call_id: &str) {
        if self
            .pending_ring
            .as_ref()
            .is_some_and(|r| r.header("Call-ID") == call_id)
        {
            self.pending_ring = None;
        }
        if self.ring_call_id == call_id {
            self.ring_call_id.clear();
        }
    }
}

/// One started run: the endpoint, the token that ends the registrar, and the latch it sets once
/// it has finished (the de-register included).
struct Run {
    endpoint: Endpoint,
    cancel: CancelToken,
    done: CancelToken,
    /// set by `stop`: the endpoint stays (a `dial` after `stop` reaches the closed socket, as in
    /// Go) but a `start` may replace the run
    stopped: bool,
}

/// Go's `Driver`.
#[derive(Default)]
pub struct Dahua {
    cfg: Config,
    #[cfg(feature = "test-seams")]
    sip_port: Option<i64>,
    ring: Arc<Mutex<RingState>>,
    run: Mutex<Option<Run>>,
    /// serialises `start` (a second one sees `already started`)
    start_lock: tokio::sync::Mutex<()>,
    /// test seam: the registrar task panics (once)
    #[cfg(feature = "test-seams")]
    panic_in_registrar: PanicFlag,
    /// test seam: the supervisor panics once its first registrar is over
    #[cfg(feature = "test-seams")]
    panic_in_supervisor: PanicFlag,
    /// the first wait before a panicked registrar is started again (doubles, capped at 5 min)
    restart_delay: Mutex<Duration>,
}

/// `New`.
pub fn new() -> Box<dyn Driver> {
    Box::new(Dahua::default())
}

/// A random 8-byte To-tag, 16 hex digits (`randTag`).
fn rand_tag() -> String {
    crate::util::rand_hex(8)
}

impl Dahua {
    /// Points this driver at a device on `port` instead of 5060 (a test seam; see [`SIP_PORT`]).
    #[cfg(feature = "test-seams")]
    pub fn set_sip_port(&mut self, port: i64) {
        self.sip_port = Some(port);
    }

    /// Test seam: the wait before a panicked registrar is restarted (default 5 s).
    #[cfg(feature = "test-seams")]
    pub fn set_restart_delay(&self, d: Duration) {
        *lock(&self.restart_delay) = d;
    }

    /// Test seam: the supervisor task itself panics when its first registrar has ended.
    #[cfg(feature = "test-seams")]
    pub fn panic_in_supervisor_for_test(&self) {
        self.panic_in_supervisor.arm();
    }

    /// Test seam: the registrar task panics as soon as it runs (once).
    #[cfg(feature = "test-seams")]
    pub fn panic_in_registrar_for_test(&self) {
        self.panic_in_registrar.arm();
    }

    /// Test seam: the ringing INVITE, without consuming it.
    #[cfg(feature = "test-seams")]
    pub fn peek_pending_ring(&self) -> Option<IncomingRequest> {
        self.ring().pending_ring.clone()
    }

    /// Test seam: Go's tests set `d.pendingRing` directly.
    #[cfg(feature = "test-seams")]
    pub fn set_pending_ring_for_test(&self, req: Option<IncomingRequest>) {
        self.ring().pending_ring = req;
    }

    /// `dropPendingRing`, exposed for the ported unit test.
    #[cfg(feature = "test-seams")]
    pub fn drop_pending_ring(&self, call_id: &str) {
        self.ring().drop_pending_ring(call_id);
    }

    fn ring(&self) -> std::sync::MutexGuard<'_, RingState> {
        lock(&self.ring)
    }

    fn endpoint(&self) -> Option<Endpoint> {
        lock(&self.run).as_ref().map(|r| r.endpoint.clone())
    }

    fn capabilities_value() -> Capabilities {
        Capabilities {
            inbound_audio: AudioTopology::SIP_RTP,
            outbound_audio: AudioTopology::SIP_RTP,
            direct_capable: true,
            has_relay: true,
            rtsp_template: "rtsp://{host}/cam/realmonitor?channel=1&subtype=1".to_string(),
            // Registration state is reported through availability_changed (start).
            reports_availability: true,
            can_dial: false,
            ..Capabilities::default()
        }
    }
}

impl Driver for Dahua {
    /// Validates and stores this device's connection details from the config-flow schema
    /// (`custom_components/portico/drivers/dahua_sip.py`); touches no network, that is `start`'s
    /// job. A non-string value counts as absent, as `cfg["host"].(string)` does; a failed
    /// validation leaves the previous configuration in place.
    fn configure(&mut self, cfg: &Map<String, Value>) -> Result<(), DriverError> {
        let get = |k: &str| match cfg.get(k) {
            Some(Value::String(s)) => s.clone(),
            _ => String::new(),
        };
        let (host, sip_user, sip_password) = (get("host"), get("sip_user"), get("sip_password"));
        // a safety net (DEV-796 D3): registered before any refusal, for any later panic text
        crate::process::panic_hook::register_secret(&sip_password);
        if host.is_empty() || sip_user.is_empty() || sip_password.is_empty() {
            return Err(DriverError::new(
                "dahua_sip: host, sip_user and sip_password are required",
            ));
        }
        self.cfg = Config {
            host,
            sip_user,
            sip_password,
        };
        Ok(())
    }

    fn capabilities(&self) -> Capabilities {
        Self::capabilities_value()
    }

    /// Opens the shared SIP endpoint for this device (one socket for both registration and
    /// inbound calls, since our REGISTER's own Contact header is the address a ring's INVITE
    /// arrives at), registers the configured `user#index` account and listens for an inbound
    /// INVITE (a ring) to report via `ringing`. Real SIP traffic against a real device: the
    /// activation-gate rule of the design doc (§7) applies from here, not from `configure`.
    fn start<'a>(
        &'a self,
        ctx: &'a CancelToken,
        events: Arc<dyn EventSink>,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move {
            let _starting = self.start_lock.lock().await;
            if lock(&self.run).as_ref().is_some_and(|r| !r.stopped) {
                return Err(DriverError::new(format!("{KEY}: already started")));
            }
            // 0 is 5060; only the tests point a driver at a fake device on another port
            #[cfg(feature = "test-seams")]
            let port = self
                .sip_port
                .unwrap_or_else(|| SIP_PORT.load(Ordering::SeqCst));
            #[cfg(not(feature = "test-seams"))]
            let port = 0;
            let endpoint = Endpoint::new(&self.cfg.host, port)
                .await
                .map_err(|e| DriverError::new(format!("dahua_sip: opening SIP endpoint: {e}")))?;
            let host = self.cfg.host.clone();

            install_handlers(&endpoint, &self.ring, &events, &host);

            let seen: Arc<Mutex<Option<bool>>> = Arc::new(Mutex::new(None));
            let make_registrar = registrar_factory(&endpoint, &self.cfg, &events, &host, &seen);

            let cancel = CancelToken::new();
            let done = CancelToken::new();
            // The hub's context ends the run too (Go: `context.WithCancel(ctx)`).
            let (parent, c2, d2) = (ctx.clone(), cancel.clone(), done.clone());
            tokio::spawn(async move {
                tokio::select! {
                    _ = parent.cancelled() => c2.cancel(),
                    _ = d2.cancelled() => {}
                }
            });
            #[cfg(feature = "test-seams")]
            let panic_now = self.panic_in_registrar.peek();
            #[cfg(feature = "test-seams")]
            let panic_supervisor = self.panic_in_supervisor.peek();
            let restart_base = match *lock(&self.restart_delay) {
                d if d.is_zero() => Duration::from_secs(5),
                d => d,
            };
            tokio::spawn(
                Supervisor {
                    make_registrar,
                    cancel: cancel.clone(),
                    done: done.clone(),
                    events: events.clone(),
                    host: host.clone(),
                    user: self.cfg.sip_user.clone(),
                    seen,
                    restart_base,
                    #[cfg(feature = "test-seams")]
                    panic_now,
                    #[cfg(feature = "test-seams")]
                    panic_supervisor,
                }
                .run(),
            );
            *lock(&self.run) = Some(Run {
                endpoint,
                cancel,
                done,
                stopped: false,
            });
            Ok(())
        })
    }

    /// De-registers the account, then closes the socket. It waits (up to [`STOP_TIMEOUT`]) for
    /// the registrar's `Expires: 0` to go out first: closing the socket first would cut off the
    /// very request that frees the device's session-quota slot (§6). Stopping twice is fine.
    fn stop(&self) -> BoxFuture<'_, Result<(), DriverError>> {
        Box::pin(async move {
            let run = {
                let g = lock(&self.run);
                g.as_ref()
                    .map(|r| (r.endpoint.clone(), r.cancel.clone(), r.done.clone()))
            };
            let Some((endpoint, cancel, done)) = run else {
                return Ok(());
            };
            cancel.cancel();
            let _ = tokio::time::timeout(STOP_TIMEOUT, done.cancelled()).await;
            let closed = endpoint
                .close()
                .map_err(|e| DriverError::new(e.to_string()));
            // the run is over: a later `start` on this instance opens a new endpoint (Go does)
            if let Some(r) = lock(&self.run).as_mut() {
                r.stopped = true;
            }
            closed
        })
    }

    /// Places an outbound call to the device itself (§9.6, e.g. proactively speaking to a
    /// visitor), offering `target` as where we want RTP. The context is not used, as in Go.
    fn dial<'a>(
        &'a self,
        _ctx: &'a CancelToken,
        target: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move {
            let Some(endpoint) = self.endpoint() else {
                return Err(DriverError::new(
                    "dahua_sip: Dial: the driver is not started",
                ));
            };
            let call = sip::dial(
                &endpoint,
                &self.cfg.sip_user,
                &self.cfg.sip_user,
                SdpInfo {
                    host: target.host,
                    port: target.port,
                    payload_type: 0,
                },
                None,
            )
            .await
            .map_err(|e| DriverError::new(format!("dahua_sip: Dial: {e}")))?;
            self.ring().active_call = Some(Arc::new(call));
            Ok(())
        })
    }

    /// Accepts the currently-ringing INVITE (set by `start`'s INVITE handler), offering `target`
    /// as where we want RTP. There is exactly one pending ring at a time per §4.5/§6's
    /// single-call-per-device model, so this needs no call id.
    fn answer<'a>(
        &'a self,
        _ctx: &'a CancelToken,
        target: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move {
            let req = self.ring().pending_ring.take();
            let Some(req) = req else {
                return Err(DriverError::new(
                    "dahua_sip: Answer called with no call ringing",
                ));
            };
            let call = sip::answer_incoming(
                &req,
                SdpInfo {
                    host: target.host,
                    port: target.port,
                    payload_type: 0,
                },
            )
            .await
            .map_err(|e| DriverError::new(format!("dahua_sip: Answer: {e}")))?;
            self.ring().active_call = Some(Arc::new(call));
            Ok(())
        })
    }

    fn open_relay<'a>(&'a self, _ctx: &'a CancelToken) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move { Err(DriverError::new(OPEN_RELAY_REFUSAL)) })
    }

    fn as_pending_ringer(&self) -> Option<&dyn PendingRinger> {
        Some(self)
    }
}

/// `OpenRelay`'s text, verbatim.
pub const OPEN_RELAY_REFUSAL: &str = "dahua_sip: OpenRelay not implemented — its real protocol for this driver (in-dialog SIP INFO vs. Dahua's own HTTP RPC2 API) needs checking against the estate's existing gate-sip/bell-talk implementation before building it, not guessing (Phase 1 follow-up)";

impl PendingRinger for Dahua {
    /// `PendingRing`: consumes and returns the currently-ringing INVITE, if any. The relay-mode
    /// counterpart to `answer` (which consumes the same ring and signals the device at once); the
    /// two are mutually exclusive consumers of the one pending ring, whichever runs first wins.
    fn pending_ring(&self) -> Option<IncomingRequest> {
        self.ring().pending_ring.take()
    }

    /// Whether the pending ring is the one of `call_id` (consumes nothing).
    fn has_pending_ring_for(&self, call_id: &str) -> bool {
        self.ring()
            .pending_ring
            .as_ref()
            .is_some_and(|r| r.header("Call-ID") == call_id)
    }

    /// `PendingRingFor`: `pending_ring` for one call. It consumes the ring only if its Call-ID is
    /// `call_id`, so with several doors in one process a browser offer takes its own call's ring
    /// and no other.
    fn pending_ring_for(&self, call_id: &str) -> Option<IncomingRequest> {
        let mut g = self.ring();
        if g.pending_ring
            .as_ref()
            .is_some_and(|r| r.header("Call-ID") == call_id)
        {
            g.pending_ring.take()
        } else {
            None
        }
    }
}

/// What the INVITE handler decided under the lock.
enum Invite {
    /// a retransmit of the ring in progress: repeat the 180 with this tag (only while the ring
    /// is still pending)
    Retransmit { tag: String, pending: bool },
    /// a new ring: report it and send the 180 with this tag
    New { tag: String },
}

/// The three SIP verbs of a ring: INVITE starts it, CANCEL and BYE end it.
fn install_handlers(
    endpoint: &Endpoint,
    ring_state: &Arc<Mutex<RingState>>,
    events: &Arc<dyn EventSink>,
    host: &str,
) {
    // INVITE: a ring.
    let (ring, ev, h) = (ring_state.clone(), events.clone(), host.to_string());
    endpoint.handle("INVITE", move |req: IncomingRequest| {
        let (ring, ev, host) = (ring.clone(), ev.clone(), h.clone());
        async move { on_invite(&ring, ev.as_ref(), &host, req).await }
    });
    let (ring, ev) = (ring_state.clone(), events.clone());
    endpoint.handle("CANCEL", move |req: IncomingRequest| {
        let (ring, ev) = (ring.clone(), ev.clone());
        async move { on_cancel(&ring, ev.as_ref(), req).await }
    });
    let (ring, ev) = (ring_state.clone(), events.clone());
    endpoint.handle("BYE", move |req: IncomingRequest| {
        let (ring, ev) = (ring.clone(), ev.clone());
        async move { on_bye(&ring, ev.as_ref(), req).await }
    });
}

/// Builds a fresh registrar for the account on every (re)start. Registration outcomes reach
/// Python as `driver_availability`, on transitions only (the registrar reports every attempt);
/// `seen` outlives a restarted registrar.
fn registrar_factory(
    endpoint: &Endpoint,
    cfg: &Config,
    events: &Arc<dyn EventSink>,
    host: &str,
    seen: &Arc<Mutex<Option<bool>>>,
) -> Box<dyn Fn() -> Registrar + Send> {
    let (ep2, user, pw) = (
        endpoint.clone(),
        cfg.sip_user.clone(),
        cfg.sip_password.clone(),
    );
    let (ev0, h0, seen0) = (events.clone(), host.to_string(), seen.clone());
    Box::new(move || {
        let mut registrar = Registrar::new(&ep2, &user, &pw, 0);
        let (ev, h, user, seen) = (ev0.clone(), h0.clone(), user.clone(), seen0.clone());
        registrar.on_state_change = Some(Box::new(move |registered, err| {
            if registered {
                out_line(&format!("dahua_sip: registered {user} on {h}"));
            } else {
                match err {
                    Some(e) => {
                        out_line(&format!("dahua_sip: registration problem for {user}: {e}"))
                    }
                    None => out_line(&format!(
                        "dahua_sip: registration problem for {user}: <nil>"
                    )),
                }
            }
            {
                let mut g = lock(&seen);
                if *g == Some(registered) {
                    return;
                }
                *g = Some(registered);
            }
            let reason = match (registered, err) {
                (false, Some(e)) => e.to_string(),
                _ => String::new(),
            };
            ev.availability_changed(&h, registered, &reason);
        }));
        registrar
    })
}

/// What the registrar supervisor owns.
struct Supervisor {
    make_registrar: Box<dyn Fn() -> Registrar + Send>,
    cancel: CancelToken,
    done: CancelToken,
    events: Arc<dyn EventSink>,
    host: String,
    user: String,
    seen: Arc<Mutex<Option<bool>>>,
    restart_base: Duration,
    #[cfg(feature = "test-seams")]
    panic_now: bool,
    #[cfg(feature = "test-seams")]
    panic_supervisor: bool,
}

impl Supervisor {
    // The supervisor: whatever happens to the registrar, `done` is set when it is over for
    // good (from a drop guard, so a panic of the supervisor itself cannot leave `stop`
    // waiting out its timeout). A failure nothing can fix (486 quota, rejected
    // credentials) reaches Python as driver_error. A PANIC (Go's would end the process)
    // is reported as unavailable, and the registrar is started again with a backoff, so
    // the device does not stay shown as available while nothing keeps it registered. The
    // log lines go through `panic_hook`, which cannot panic on a closed stream.
    async fn run(self) {
        let Supervisor {
            make_registrar,
            cancel: c3,
            done: d3,
            events: ev,
            host: h,
            user,
            seen,
            restart_base,
            #[cfg(feature = "test-seams")]
            panic_now,
            #[cfg(feature = "test-seams")]
            panic_supervisor,
        } = self;
        struct Latch(CancelToken);
        impl Drop for Latch {
            fn drop(&mut self) {
                self.0.cancel();
            }
        }
        let _latch = Latch(d3);
        let mut failures: u32 = 0;
        #[cfg(feature = "test-seams")]
        let mut first = true;
        loop {
            let registrar = make_registrar();
            let c = c3.clone();
            #[cfg(feature = "test-seams")]
            let panic_this = panic_now && first;
            #[cfg(feature = "test-seams")]
            {
                first = false;
            }
            let started = tokio::time::Instant::now();
            let worker = tokio::spawn(async move {
                #[cfg(feature = "test-seams")]
                if panic_this {
                    panic!("dahua_sip test panic in the registrar");
                }
                registrar.run(&c).await
            });
            let ended = worker.await;
            #[cfg(feature = "test-seams")]
            if panic_supervisor {
                panic!("dahua_sip test panic in the supervisor");
            }
            match ended {
                Ok(Ok(())) => out_line(&format!("dahua_sip: de-registered {user}")),
                Ok(Err(e)) => {
                    out_line(&format!("dahua_sip: registrar for {user} stopped: {e}"));
                    if let Some(er) = ev.error_reporter() {
                        er.driver_error(&h, "registration_failed", &e.to_string());
                    }
                }
                // nothing aborts the worker: not a panic means the run is over
                Err(je) if !je.is_panic() => {
                    log_line(&format!(
                        "portico-transport: dahua_sip: registrar for {user} was cancelled"
                    ));
                }
                Err(je) => {
                    log_line(&format!(
                        "portico-transport: dahua_sip: registrar for {user} panicked: {}",
                        crate::util::join_panic_message(je)
                    ));
                    {
                        let mut g = lock(&seen);
                        if *g != Some(false) {
                            *g = Some(false);
                            ev.availability_changed(&h, false, "internal error");
                        }
                    }
                    let wait;
                    (failures, wait) =
                        restart_plan(failures, started.elapsed(), restart_base, RESTART_STABLE);
                    tokio::select! {
                        _ = c3.cancelled() => {}
                        _ = tokio::time::sleep(wait) => continue,
                    }
                }
            }
            break;
        }
    }
}

fn classify_invite(ring: &Mutex<RingState>, call_id: &str, req: &mut IncomingRequest) -> Invite {
    let mut g = lock(ring);
    if g.ring_call_id == call_id {
        // A retransmitted INVITE (RFC 3261 §17.2.1): same transaction, so no second Ringing and
        // no new pending ring; just repeat the 180 while the ring is still unanswered. Once
        // answered or cancelled, ignore it.
        return Invite::Retransmit {
            tag: g.ring_tag.clone(),
            pending: g.pending_ring.is_some(),
        };
    }
    req.to_tag = rand_tag();
    g.pending_ring = Some(req.clone());
    g.ring_call_id = call_id.to_string();
    g.ring_tag = req.to_tag.clone();
    Invite::New {
        tag: req.to_tag.clone(),
    }
}

async fn on_invite(
    ring: &Mutex<RingState>,
    events: &dyn EventSink,
    host: &str,
    mut req: IncomingRequest,
) {
    let call_id = req.header("Call-ID").to_string();
    match classify_invite(ring, &call_id, &mut req) {
        Invite::Retransmit { tag, pending } => {
            if pending {
                let _ = req.respond(180, "Ringing", &tag, "").await;
            }
        }
        Invite::New { tag } => {
            events.ringing(&call_id, host);
            // provisional only: Answer sends the real 200 OK
            let _ = req.respond(180, "Ringing", &tag, "").await;
        }
    }
}

async fn on_cancel(ring: &Mutex<RingState>, events: &dyn EventSink, req: IncomingRequest) {
    let call_id = req.header("Call-ID").to_string();
    let taken = {
        let mut g = lock(ring);
        match g.pending_ring.take() {
            Some(r) if r.header("Call-ID") == call_id => Some((r, g.ring_tag.clone())),
            other => {
                // not this call's: put it back untouched
                g.pending_ring = other;
                None
            }
        }
        // ring_call_id stays set, so a late INVITE retransmit is not a new ring
    };
    let Some((invite, tag)) = taken else {
        // Nothing left to cancel (never rang, already answered or ended).
        let _ = req
            .respond(481, "Call/Transaction Does Not Exist", "", "")
            .await;
        return;
    };
    // RFC 3261 §9.2: 200 to the CANCEL, then 487 to the INVITE it cancels.
    let _ = req.respond(200, "OK", &tag, "").await;
    let _ = invite.respond(487, "Request Terminated", &tag, "").await;
    events.state_changed(&call_id, STATE_ENDED);
}

async fn on_bye(ring: &Mutex<RingState>, events: &dyn EventSink, req: IncomingRequest) {
    let _ = req.respond(200, "OK", "", "").await;
    let call_id = req.header("Call-ID").to_string();
    {
        let mut g = lock(ring);
        g.active_call = None;
        g.drop_pending_ring(&call_id);
    }
    // The station hung up: tell the core, which also closes any relay it joined to this call
    // (the relay-closing sink).
    events.state_changed(&call_id, STATE_ENDED);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sip::{parse_request, IncomingRequest};

    /// A ringing INVITE that arrived on no endpoint, with only a Call-ID (`pending_test.go`'s
    /// `&sip.IncomingRequest{ParsedRequest: ...{"call-id": {id}}}`).
    fn req(id: &str) -> IncomingRequest {
        let raw = format!("INVITE sip:8001@h SIP/2.0\r\nCall-ID: {id}\r\n\r\n");
        IncomingRequest::detached(parse_request(raw.as_bytes()).unwrap())
    }

    fn id_of(r: &IncomingRequest) -> String {
        r.header("Call-ID").to_string()
    }

    // TestPendingRingForMatchesTheCallId
    #[test]
    fn pending_ring_for_matches_the_call_id() {
        let d = Dahua::default();
        d.set_pending_ring_for_test(Some(req("call-a")));
        assert!(
            d.pending_ring_for("call-b").is_none(),
            "PendingRingFor(call-b) took call-a's ring"
        );
        assert!(
            d.peek_pending_ring().is_some(),
            "a non-matching PendingRingFor consumed the ring"
        );
        let r = d.pending_ring_for("call-a").expect("call-a's ring");
        assert_eq!(id_of(&r), "call-a");
        assert!(d.peek_pending_ring().is_none());
        assert!(
            d.pending_ring_for("call-a").is_none(),
            "the ring was handed out twice"
        );
    }

    // TestDropPendingRingOnlyForTheRingingCall
    #[test]
    fn drop_pending_ring_only_for_the_ringing_call() {
        let d = Dahua::default();
        d.set_pending_ring_for_test(Some(req("call-a")));
        d.drop_pending_ring("call-b");
        assert!(
            d.peek_pending_ring().is_some(),
            "a BYE for call-b dropped call-a's ring"
        );
        d.drop_pending_ring("call-a");
        assert!(
            d.peek_pending_ring().is_none(),
            "a BYE for call-a left its ring pending"
        );
        assert!(
            d.pending_ring_for("call-a").is_none(),
            "a dead INVITE was handed out"
        );
        d.drop_pending_ring("call-a"); // nil ring: no panic
    }

    // PendingRing takes whichever ring is there, once; the two takers are exclusive.
    #[test]
    fn pending_ring_takes_whichever_ring_is_there_once() {
        let d = Dahua::default();
        assert!(d.pending_ring().is_none());
        d.set_pending_ring_for_test(Some(req("call-z")));
        assert_eq!(id_of(&d.pending_ring().unwrap()), "call-z");
        assert!(d.pending_ring().is_none());
        assert!(d.pending_ring_for("call-z").is_none());
    }

    #[test]
    fn configure_stores_the_account_and_a_failed_one_keeps_the_old() {
        let mut d = Dahua::default();
        let ok: Map<String, Value> = serde_json::from_str(
            r#"{"host":"h","sip_user":"u#1","sip_password":"p","rtsp_username":"r","rtsp_password":"s"}"#,
        )
        .unwrap();
        d.configure(&ok).unwrap();
        assert_eq!(d.cfg.host, "h");
        let bad: Map<String, Value> = serde_json::from_str(r#"{"host":"other"}"#).unwrap();
        let e = d.configure(&bad).unwrap_err();
        assert_eq!(
            e.message(),
            "dahua_sip: host, sip_user and sip_password are required"
        );
        assert_eq!(d.cfg.host, "h", "a failed Configure replaced the account");
        assert_eq!(d.cfg.sip_user, "u#1");
    }

    #[test]
    fn capabilities_are_gos() {
        assert_eq!(
            Dahua::default().capabilities().to_wire(),
            r#"{"InboundAudio":0,"OutboundAudio":0,"DirectCapable":true,"HasRelay":true,"RTSPTemplate":"rtsp://{host}/cam/realmonitor?channel=1\u0026subtype=1","ReportsAvailability":true}"#
        );
    }
}
