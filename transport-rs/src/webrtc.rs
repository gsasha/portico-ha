//! The `webrtc_offer` / `webrtc_answer` / `webrtc_ice` handlers and the `sessionRegistry` of
//! `cmd/portico-transport/main.go` (DEV-764 slice 4b), plus `joinCallToBrowser` and the two
//! bridges that exist for the drivers transport-rs has: a call a driver answered itself
//! (`AudioClaimer`, gate_sip_observer) and a ring a driver answers once the browser connected
//! (`ConnectAnswerer`, gate_sip_server), and since slice 6 the pending-ring relay of dahua_sip
//! (`PendingRinger`, `call::Relay`, [`RelayRegistry`], [`RelayClosingSink`]), and since slice 7b the
//! talkback bridge of a Protect ring (`TalkbackAnswerer`, `findTalkbackAnswerer` /
//! `answerViaTalkback`: [`find_talkback_answerer`], the talkback branch of
//! [`join_call_to_browser_with`]).

use crate::util::lock;
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use serde_json::json;

use crate::call::{answer_once_connected, forward, open_talkback_once_connected, ClientLeg, Relay};
use crate::cancel::CancelToken;
use crate::dispatch::{bad_params, Hub, WEBRTC_PARAMS};
use crate::driver::{
    CallAudio, CallReasonSink, CallStatsSink, CapabilitiesSink, Driver, ErrorReporter, EventSink, ListenStateSink,
    STATE_ENDED,
};
use crate::gojson;
use crate::ipc::{raw_str, HandlerResult, IpcError, Params};
use crate::sfu::session::{Session, SessionConfig};
use crate::sip::IncomingRequest;

/// `sessionRegistry`: the in-progress browser sessions by call id, and which of them already had
/// their device leg joined.
pub struct SessionRegistry {
    cfg: SessionConfig,
    inner: Mutex<Inner>,
    /// set by `close_all_wait`: the transport is stopping and no session is created any more
    stopping: AtomicBool,
}

#[derive(Default)]
struct Inner {
    sessions: HashMap<String, Arc<Session>>,
    /// the session whose device leg was joined, per call id: a renegotiating offer reuses the
    /// session and must not join the same call twice
    joined: HashMap<String, Arc<Session>>,
}

impl SessionRegistry {
    pub fn new(cfg: SessionConfig) -> Arc<Self> {
        Arc::new(SessionRegistry {
            cfg,
            inner: Mutex::new(Inner::default()),
            stopping: AtomicBool::new(false),
        })
    }

    fn inner(&self) -> std::sync::MutexGuard<'_, Inner> {
        lock(&self.inner)
    }

    /// `markJoined`: true for the first session to claim `call_id`'s join.
    pub fn mark_joined(&self, call_id: &str, s: &Arc<Session>) -> bool {
        let mut g = self.inner();
        // a session that already left the table (it died while its offer was being answered)
        // must not leave a `joined` entry behind: nothing would ever remove it
        if !g.sessions.get(call_id).is_some_and(|c| Arc::ptr_eq(c, s)) || s.is_done() {
            return false;
        }
        if g.joined.get(call_id).is_some_and(|j| Arc::ptr_eq(j, s)) {
            return false;
        }
        g.joined.insert(call_id.to_string(), s.clone());
        true
    }

    /// `getOrCreate`. The browser leg going away for good releases the session and frees the
    /// table slot (`OnDisconnected`).
    pub fn get_or_create(self: &Arc<Self>, call_id: &str) -> Result<Arc<Session>, String> {
        let mut g = self.inner();
        if let Some(s) = g.sessions.get(call_id) {
            return Ok(s.clone());
        }
        // checked under the table lock, which `close_all_wait` also takes after setting the flag:
        // an offer queued behind the shutdown cannot create a session nobody will close (R1-107)
        if self.stopping.load(Ordering::SeqCst) {
            return Err("sfu: the transport is stopping".to_string());
        }
        if g.sessions.len() >= self.cfg.max_sessions {
            return Err(format!(
                "sfu: too many concurrent sessions ({} is the limit, PORTICO_SFU_MAX_SESSIONS)",
                self.cfg.max_sessions
            ));
        }
        let s = Session::with_config(call_id, self.cfg.clone())?;
        g.sessions.insert(call_id.to_string(), s.clone());
        drop(g);
        let (reg, id, weak) = (self.clone(), call_id.to_string(), Arc::downgrade(&s));
        s.on_disconnected(move || {
            if let Some(s) = weak.upgrade() {
                reg.remove(&id, &s);
                s.close();
            }
        });
        Ok(s)
    }

    /// `remove`: drops `call_id`'s session, but only if it is still `s`.
    pub fn remove(&self, call_id: &str, s: &Arc<Session>) {
        let mut g = self.inner();
        if g.sessions.get(call_id).is_some_and(|c| Arc::ptr_eq(c, s)) {
            g.sessions.remove(call_id);
        }
        if g.joined.get(call_id).is_some_and(|c| Arc::ptr_eq(c, s)) {
            g.joined.remove(call_id);
        }
    }

    pub fn get(&self, call_id: &str) -> Option<Arc<Session>> {
        self.inner().sessions.get(call_id).cloned()
    }

    pub fn len(&self) -> usize {
        self.inner().sessions.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// `joined` entries (a test seam for the leak race).
    #[cfg(feature = "test-seams")]
    pub fn joined_len(&self) -> usize {
        self.inner().joined.len()
    }

    /// Closes every session (the transport is stopping) and waits, at most `wait` in all, for each
    /// to send its close_notify and finish, so the station's BYE leaves before the process does.
    pub async fn close_all_wait(&self, wait: std::time::Duration) {
        self.stopping.store(true, Ordering::SeqCst);
        let all: Vec<Arc<Session>> = self.inner().sessions.values().cloned().collect();
        for s in &all {
            s.close();
        }
        let end = tokio::time::Instant::now() + wait;
        for s in &all {
            let _ = tokio::time::timeout_at(end, s.wait_done()).await;
        }
    }
}

/// `findClaimedAudio`: the audio of `call_id` if some driver answered that call itself.
pub fn find_claimed_audio(
    drivers: &[Arc<dyn Driver>],
    call_id: &str,
) -> Option<Arc<dyn CallAudio>> {
    drivers
        .iter()
        .find_map(|d| d.as_audio_claimer().and_then(|c| c.claimed_audio(call_id)))
}

/// `copyFrames`: forwards frames until `src` closes; a full `dst` drops the new frame.
async fn copy_frames(a: Arc<dyn CallAudio>, leg: Arc<dyn ClientLeg>) {
    forward(a.output(), leg.input()).await
}

/// The body of `bridgeClaimedAudio` / `pumpCallAudio`: both ways until the driver closes its
/// output (the browser's output is never closed, so the uplink copy ends with the downlink one).
fn pump(a: Arc<dyn CallAudio>, leg: Arc<dyn ClientLeg>) {
    let over = CancelToken::new();
    let (a1, l1, o1) = (a.clone(), leg.clone(), over.clone());
    tokio::spawn(async move {
        copy_frames(a1, l1).await;
        o1.cancel();
    });
    tokio::spawn(async move {
        let src = leg.output();
        let (mut frames, mut dropped) = (0u64, 0u64);
        loop {
            let frame = tokio::select! {
                biased;
                _ = over.cancelled() => break,
                f = async { src.lock().await.recv().await } => f,
            };
            match frame {
                None => break,
                Some(f) => {
                    frames += 1;
                    if a.input().try_send(f).is_err() {
                        dropped += 1;
                    }
                }
            }
        }
        if dropped > 0 {
            crate::panic_hook::out_line(&format!("portico-transport: sfu browser to device: {dropped} of {frames} frames dropped (the device queue was full)"));
        }
    });
}

/// `bridgeClaimedAudio`: a call a driver answered itself (gate_sip_observer, claims on) joins
/// the browser's session once it connects; the driver is told when the leg takes the audio
/// (`attach`) and when it is gone (`detach`, which hangs the call up).
pub fn bridge_claimed_audio(a: Arc<dyn CallAudio>, leg: Arc<dyn ClientLeg>) {
    let (a1, l1) = (a.clone(), leg.clone());
    leg.on_connected(Box::new(move || {
        a1.attach();
        pump(a1, l1);
    }));
    leg.on_disconnected(Box::new(move || a.detach()));
}

/// `findTalkbackAnswerer`: the driver holding `call_id` as a live, unanswered ring that is answered
/// through a talkback session (unifi_protect). Matched by call id, so it is exact.
pub fn find_talkback_answerer(
    drivers: &[Arc<dyn Driver>],
    call_id: &str,
) -> Option<Arc<dyn Driver>> {
    drivers
        .iter()
        .find(|d| {
            d.as_talkback_answerer()
                .is_some_and(|ta| ta.ringing_call(call_id))
        })
        .cloned()
}

/// `answerViaTalkback`: joins a Protect ring to the browser: nothing is opened on the console
/// until the browser leg has actually connected (§9.3, §9.6), then the leg's audio goes into the
/// talkback session. A browser that goes away after the call was taken ends it; one that never
/// connects leaves the ring answerable for someone else.
///
/// Rust adds (PARITY.md 147): a relay whose browser left while the session was opening is not
/// kept in the registry (Go's `put` after the disconnect would leave a dead relay there until
/// the call's end), and a failed open that the driver reports is logged without the console's
/// text beyond the driver's own scrubbed message.
fn answer_via_talkback(
    ctx: CancelToken,
    d: Arc<dyn Driver>,
    call_id: String,
    leg: Arc<dyn ClientLeg>,
    relays: Arc<RelayRegistry>,
) {
    let relay = Arc::new(Relay::with_call_id(&call_id));
    // Go's `gone` flag, set synchronously by the disconnect callback (R2-81)
    let gone = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let result = {
        let (d, id, ctx) = (d.clone(), call_id.clone(), ctx);
        open_talkback_once_connected(
            relay.clone(),
            move || async move {
                let Some(ta) = d.as_talkback_answerer() else {
                    return Err("the driver no longer answers through a talkback".to_string());
                };
                ta.answer_talkback(&ctx, &id)
                    .await
                    .map_err(|e| e.message().to_string())
            },
            leg.clone(),
        )
    };
    {
        let (d, id, relay, relays, gone) = (
            d.clone(),
            call_id.clone(),
            relay.clone(),
            relays.clone(),
            gone.clone(),
        );
        leg.on_disconnected(Box::new(move || {
            gone.store(true, std::sync::atomic::Ordering::SeqCst); // before anything else (R2-81)
            let Some(ta) = d.as_talkback_answerer() else {
                return;
            };
            if ta.ringing_call(&id) {
                return; // never answered: the device is still ringing
            }
            let _ = ta.hangup_call(&id);
            relays.remove(&id, &relay);
            relays.close_in_background(relay);
        }));
    }
    tokio::spawn(async move {
        match result.await {
            Ok(Ok(())) => {
                relays.put(&call_id, relay.clone());
                // a disconnect that fired before the put already ran its remove (a no-op then)
                if gone.load(std::sync::atomic::Ordering::SeqCst) || relay.is_closed() {
                    relays.remove(&call_id, &relay);
                }
            }
            Ok(Err(e)) => crate::panic_hook::out_line(&format!(
                "portico-transport: talkback answer for {call_id} failed: {e}"
            )),
            // the leg never connected (its callbacks were dropped)
            Err(_) => {}
        }
    });
}

/// `answerViaConnectedAudio`: nothing is answered until the browser leg has actually connected
/// (the 200 OK goes out then), then the call's audio is bridged both ways. A browser that never
/// connects leaves the ring answerable; one that goes away after the answer hangs the call up; a
/// second browser that loses the race to answer is told so in the log and, going away, hangs up
/// nothing (it never owned the call).
fn answer_via_connected_audio(
    ctx: CancelToken,
    d: Arc<dyn Driver>,
    call_id: String,
    leg: Arc<dyn ClientLeg>,
) {
    struct St {
        audio: Option<Arc<dyn CallAudio>>,
        gone: bool,
    }
    let st = Arc::new(Mutex::new(St {
        audio: None,
        gone: false,
    }));
    let (st1, l1, d1, id1) = (st.clone(), leg.clone(), d, call_id);
    leg.on_connected(Box::new(move || {
        tokio::spawn(async move {
            let Some(ca) = d1.as_connect_answerer() else {
                return;
            };
            let a = match ca.answer_with_audio(&ctx, &id1).await {
                Ok(a) => a,
                Err(e) => {
                    crate::panic_hook::out_line(&format!(
                        "portico-transport: answer for {id1} failed: {}",
                        e.message()
                    ));
                    return;
                }
            };
            {
                let mut g = lock(&st1);
                if g.gone {
                    // the browser left while the answer was going out
                    drop(g);
                    a.detach();
                    return;
                }
                g.audio = Some(a.clone());
            }
            a.attach();
            pump(a, l1);
        });
    }));
    leg.on_disconnected(Box::new(move || {
        let a = {
            let mut g = lock(&st);
            g.gone = true;
            g.audio.clone()
        };
        if let Some(a) = a {
            a.detach(); // hang up: the browser that owned the call is gone
        }
    }));
}

/// `relayRegistry`: the relay-mode device legs actually joined to a browser session, by call id.
/// Filled when the device answer succeeded; a relay leaves it when its browser goes away or the
/// station ends the call.
#[derive(Default)]
pub struct RelayRegistry {
    relays: Mutex<HashMap<String, Arc<Relay>>>,
    /// relays whose close was started by a callback and has not finished: `close_all` waits for
    /// them too, so a shutdown does not overtake a BYE that is on its way
    closing: Mutex<Vec<Arc<Relay>>>,
}

impl RelayRegistry {
    pub fn new() -> Arc<Self> {
        Arc::new(RelayRegistry::default())
    }

    fn map(&self) -> std::sync::MutexGuard<'_, HashMap<String, Arc<Relay>>> {
        lock(&self.relays)
    }

    pub fn put(&self, call_id: &str, relay: Arc<Relay>) {
        self.map().insert(call_id.to_string(), relay);
    }

    /// `remove`: drops `call_id`'s relay, but only if it is still `relay`.
    pub fn remove(&self, call_id: &str, relay: &Arc<Relay>) {
        let mut g = self.map();
        if g.get(call_id).is_some_and(|r| Arc::ptr_eq(r, relay)) {
            g.remove(call_id);
        }
    }

    /// `take`: removes and returns the relay joined for `call_id`, if any.
    pub fn take(&self, call_id: &str) -> Option<Arc<Relay>> {
        self.map().remove(call_id)
    }

    pub fn len(&self) -> usize {
        self.map().len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    fn closing(&self) -> std::sync::MutexGuard<'_, Vec<Arc<Relay>>> {
        lock(&self.closing)
    }

    /// Closes `relay` on a task of its own (the caller does not wait for the BYE's answer) and
    /// keeps it on the `closing` list until that is done, for `close_all` to wait on. Without a
    /// runtime nothing can be sent: that is logged, not silent.
    pub fn close_in_background(self: &Arc<Self>, relay: Arc<Relay>) {
        let Ok(rt) = tokio::runtime::Handle::try_current() else {
            crate::panic_hook::err_line(
                "portico-transport: warning: no runtime to close a relay on; its BYE is not sent",
            );
            return;
        };
        self.closing().push(relay.clone());
        let reg = self.clone();
        rt.spawn(async move {
            if let Err(e) = relay.close().await {
                crate::panic_hook::out_line(&format!(
                    "portico-transport: closing a relay failed: {e}"
                ));
            }
            reg.closing().retain(|r| !Arc::ptr_eq(r, &relay));
        });
    }

    /// Closes every relay (the transport is stopping), all at once, waiting at most `wait` in
    /// all, so each station gets its BYE before the process exits. Relays whose close a callback
    /// already started are waited for too (`Relay::close` makes a second caller wait for the
    /// first).
    pub async fn close_all(&self, wait: std::time::Duration) {
        let mut all: Vec<Arc<Relay>> = self.map().drain().map(|(_, r)| r).collect();
        all.extend(self.closing().iter().cloned());
        let mut set = tokio::task::JoinSet::new();
        for r in all {
            set.spawn(async move {
                let _ = r.close().await;
            });
        }
        let _ =
            tokio::time::timeout(wait, async { while set.join_next().await.is_some() {} }).await;
    }
}

/// `relayClosingSink`: forwards every event and, when a call ends from the device side (a station
/// BYE), also closes and forgets the relay joined to it, so the audio session and its tasks do
/// not outlive the call. The close runs on a task of its own (Go's runs in the reporting
/// goroutine, which then waits for the BYE's answer). Each optional extension of the wrapped sink
/// is forwarded: a wrapper that forgot one would silence it for every driver behind it.
pub struct RelayClosingSink {
    inner: Arc<dyn EventSink>,
    relays: Arc<RelayRegistry>,
}

impl RelayClosingSink {
    pub fn new(inner: Arc<dyn EventSink>, relays: Arc<RelayRegistry>) -> Self {
        RelayClosingSink { inner, relays }
    }
}

impl EventSink for RelayClosingSink {
    fn ringing(&self, call_id: &str, device_id: &str) {
        self.inner.ringing(call_id, device_id);
    }
    fn state_changed(&self, call_id: &str, state: &str) {
        if state == STATE_ENDED {
            if let Some(relay) = self.relays.take(call_id) {
                self.relays.close_in_background(relay);
            }
        }
        self.inner.state_changed(call_id, state);
    }
    fn availability_changed(&self, device_id: &str, available: bool, reason: &str) {
        self.inner
            .availability_changed(device_id, available, reason);
    }
    fn listen_state_sink(&self) -> Option<&dyn ListenStateSink> {
        self.inner.listen_state_sink()
    }
    fn capabilities_sink(&self) -> Option<&dyn CapabilitiesSink> {
        self.inner.capabilities_sink()
    }
    fn call_reason_sink(&self) -> Option<&dyn CallReasonSink> {
        self.inner.call_reason_sink()
    }
    fn error_reporter(&self) -> Option<&dyn ErrorReporter> {
        self.inner.error_reporter()
    }
    fn call_stats_sink(&self) -> Option<&dyn CallStatsSink> {
        self.inner.call_stats_sink()
    }
}

/// `findPendingRing`: the ring waiting to be answered for `call_id`. With several doors in one
/// process the offer names its call, so only that call's ring is taken; an empty `call_id` keeps
/// the old single-active-ring scan of every configured device. Drivers are visited in the order
/// given (the hub sorts them by device id; Go's map order is random).
pub fn find_pending_ring(drivers: &[Arc<dyn Driver>], call_id: &str) -> Option<IncomingRequest> {
    for d in drivers {
        if let Some(pr) = d.as_pending_ringer() {
            let req = if call_id.is_empty() {
                pr.pending_ring()
            } else {
                pr.pending_ring_for(call_id)
            };
            if req.is_some() {
                return req;
            }
        }
    }
    None
}

/// `joinCallToBrowser` without a relay registry or a `-host`: what the earlier slices called. A
/// ring of dahua_sip would be answered with the routed address and tracked in a throwaway table.
/// Only the tests call it (the hub goes through [`join_call_to_browser_with`]), so it is a test
/// seam (R2-130).
#[cfg(feature = "test-seams")]
pub fn join_call_to_browser(
    ctx: &CancelToken,
    drivers: &[Arc<dyn Driver>],
    call_id: &str,
    leg: Arc<dyn ClientLeg>,
) {
    join_call_to_browser_with(ctx, drivers, &RelayRegistry::new(), "", call_id, leg);
}

/// `joinCallToBrowser`: joins whatever device leg belongs to `call_id` to the browser's leg, in
/// this order: a call a driver answered itself (claimed audio), a Protect ring answered through a
/// talkback session (`TalkbackAnswerer`, matched by call id), a ring a driver answers with
/// in-process audio once the browser connects (`ConnectAnswerer`, matched by call id), then the
/// single pending SIP ring (`PendingRinger`, dahua_sip: relayed through a [`Relay`]). Nothing is
/// answered or opened before the browser leg connects.
pub fn join_call_to_browser_with(
    ctx: &CancelToken,
    drivers: &[Arc<dyn Driver>],
    relays: &Arc<RelayRegistry>,
    our_host: &str,
    call_id: &str,
    leg: Arc<dyn ClientLeg>,
) {
    if let Some(a) = find_claimed_audio(drivers, call_id) {
        bridge_claimed_audio(a, leg);
    } else if let Some(d) = find_talkback_answerer(drivers, call_id) {
        // A Protect ring (unifi_protect, ring_events on): answered by a REST talkback session
        // opened once the browser has connected.
        answer_via_talkback(ctx.clone(), d, call_id.to_string(), leg, relays.clone());
    } else if let Some(d) = drivers.iter().find(|d| {
        d.as_connect_answerer()
            .is_some_and(|ca| ca.ringing_call(call_id))
    }) {
        answer_via_connected_audio(ctx.clone(), d.clone(), call_id.to_string(), leg);
    } else if let Some(req) = find_pending_ring(drivers, call_id) {
        relay_pending_ring(relays, our_host, call_id, req, leg);
    }
}

/// The pending-ring branch of `joinCallToBrowser`. The browser leg going away ends the device
/// leg (BYE to the station, audio session closed) and frees the relay slot; a disconnect that
/// lands before the device answer completes is covered by the relay itself, which tears a leg
/// down that comes up after its `close`; `gone`, set by the disconnect callback itself, is Go's
/// flag, and decides not to register a dead relay (`relay.is_closed()` alone is set only when
/// the spawned close first runs, R2-81).
fn relay_pending_ring(
    relays: &Arc<RelayRegistry>,
    our_host: &str,
    call_id: &str,
    req: IncomingRequest,
    leg: Arc<dyn ClientLeg>,
) {
    let relay = Arc::new(Relay::new());
    // Go's `gone` flag (R2-81): set synchronously by the disconnect callback. `relay.is_closed()`
    // is only set when the spawned close first runs, which can be after the result task looked.
    let gone = Arc::new(std::sync::atomic::AtomicBool::new(false));
    {
        let (relay, relays, id, gone) = (
            relay.clone(),
            relays.clone(),
            call_id.to_string(),
            gone.clone(),
        );
        leg.on_disconnected(Box::new(move || {
            gone.store(true, std::sync::atomic::Ordering::SeqCst);
            relays.remove(&id, &relay);
            relays.close_in_background(relay);
        }));
    }
    let (r2, host) = (relay.clone(), our_host.to_string());
    let result = answer_once_connected(leg, move || async move {
        r2.answer_device(&req, &host).await?;
        r2.device_leg()
            .ok_or_else(|| "the relay was closed while answering".to_string())
    });
    let (relays, id) = (relays.clone(), call_id.to_string());
    tokio::spawn(async move {
        match result.await {
            Ok(Ok(())) => {
                let left = || gone.load(std::sync::atomic::Ordering::SeqCst) || relay.is_closed();
                if left() {
                    // the browser left while the answer was going out
                    let _ = relay.close().await;
                    return;
                }
                relays.put(&id, relay.clone());
                // A disconnect that fired between the check and the put already ran its
                // remove (a no-op then); take it back out so a dead relay is never registered.
                if left() {
                    relays.remove(&id, &relay);
                }
            }
            other => {
                if let Some(line) = relay_outcome_line(&id, &other) {
                    crate::panic_hook::out_line(&line);
                }
            }
        }
    });
}

/// The log line for a relay answer that did not complete. A failed answer is a fault; a sender
/// that was dropped (`Err`) is a leg that never connected, the normal "browser gave up", and Go
/// prints nothing for it either (R1-81).
fn relay_outcome_line(
    id: &str,
    r: &Result<Result<(), String>, tokio::sync::oneshot::error::RecvError>,
) -> Option<String> {
    match r {
        Ok(Err(e)) => Some(format!(
            "portico-transport: relay answer for {id} failed: {e}"
        )),
        Ok(Ok(())) | Err(_) => None,
    }
}

impl Hub {
    /// `webrtcOfferHandler`.
    pub(crate) async fn webrtc_offer(&self, params: Params) -> HandlerResult {
        let v =
            gojson::decode(raw_str(&params), "webrtcParams", &WEBRTC_PARAMS).map_err(bad_params)?;
        let (call_id, sdp) = (gojson::str_at(&v, 0), gojson::str_at(&v, 1));
        if call_id.is_empty() || sdp.is_empty() {
            return Err(bad_params("webrtc_offer needs call_id and sdp"));
        }
        let sessions = self.sessions();
        let started = std::time::Instant::now();
        crate::panic_hook::log_line(&format!(
            "portico-transport: webrtc_offer received call={call_id} sdp_bytes={}",
            sdp.len()
        ));
        let session = sessions.get_or_create(&call_id).map_err(|e| {
            crate::panic_hook::log_line(&format!(
                "portico-transport: webrtc_offer refused call={call_id}: could not create a session: {e}"
            ));
            IpcError::new("webrtc_failed", e)
        })?;
        let answer = match session.handle_offer(&sdp).await {
            Ok(a) => {
                crate::panic_hook::log_line(&format!(
                    "portico-transport: webrtc_offer answered call={call_id} in {} ms",
                    started.elapsed().as_millis()
                ));
                a
            }
            Err(e) => {
                crate::panic_hook::log_line(&format!(
                    "portico-transport: webrtc_offer failed call={call_id} after {} ms: {e}",
                    started.elapsed().as_millis()
                ));
                // a session that never had an offer accepted is dropped at once: it would hold
                // its sockets and a table slot for the whole connect timeout (Go keeps it)
                if !session.had_offer() {
                    sessions.remove(&call_id, &session);
                    session.close();
                }
                return Err(IpcError::new("webrtc_failed", e));
            }
        };
        // The never-dial-before-the-client-leg-lands rule (9.3, 9.6): if a device is waiting to
        // be answered, the actual SIP answer is deferred until this session reports Connected.
        // A renegotiating offer for the same call reuses the session and is answered above, but
        // the call is joined only once.
        if sessions.mark_joined(&call_id, &session) {
            join_call_to_browser_with(
                &self.ctx_token(),
                &self.all(),
                self.relays(),
                self.our_host(),
                &call_id,
                session,
            );
        }
        Ok(json!({ "sdp": answer }).into())
    }

    /// `webrtc_answer`.
    pub(crate) async fn webrtc_answer(&self, params: Params) -> HandlerResult {
        let v =
            gojson::decode(raw_str(&params), "webrtcParams", &WEBRTC_PARAMS).map_err(bad_params)?;
        let call_id = gojson::str_at(&v, 0);
        let Some(session) = self.sessions().get(&call_id) else {
            return Err(IpcError::new("unknown_session", call_id));
        };
        session
            .handle_answer(&gojson::str_at(&v, 1))
            .await
            .map_err(|e| IpcError::new("webrtc_failed", e))?;
        Ok(json!({"ok": true}).into())
    }

    /// `webrtc_ice`.
    pub(crate) async fn webrtc_ice(&self, params: Params) -> HandlerResult {
        let v =
            gojson::decode(raw_str(&params), "webrtcParams", &WEBRTC_PARAMS).map_err(bad_params)?;
        let call_id = gojson::str_at(&v, 0);
        let Some(session) = self.sessions().get(&call_id) else {
            crate::panic_hook::log_line(&format!(
                "portico-transport: webrtc_ice refused call={call_id}: unknown_session"
            ));
            return Err(IpcError::new("unknown_session", call_id));
        };
        session
            .add_ice_candidate(&gojson::str_at(&v, 2))
            .await
            .map_err(|e| IpcError::new("webrtc_failed", e))?;
        Ok(json!({"ok": true}).into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_browser_that_never_connected_is_not_reported_as_a_fault() {
        let (tx, rx) = tokio::sync::oneshot::channel::<Result<(), String>>();
        drop(tx); // the leg's callbacks were dropped with the session
        let r = rx.await;
        assert_eq!(relay_outcome_line("c1", &r), None);
        let failed: Result<Result<(), String>, _> = Ok(Err("boom".to_string()));
        assert_eq!(
            relay_outcome_line("c1", &failed).as_deref(),
            Some("portico-transport: relay answer for c1 failed: boom")
        );
    }
}
