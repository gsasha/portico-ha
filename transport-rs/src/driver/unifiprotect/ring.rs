//! `ring.go`: ring detection (design doc §9.3: "a direct subscription in the Go driver").
//!
//! The source is the console's PUBLIC Integration API events websocket,
//!
//! ```text
//! GET wss://<console>/proxy/protect/integration/v1/subscribe/events
//! X-API-KEY: <key>
//! ```
//!
//! which broadcasts `{"type":"add"|"update","item":{id, modelKey:"event", type, start, end?,
//! device, ...}}`. A doorbell press is an event with type `ring`. The public stream has no
//! "cancelled" or "answered elsewhere" signal, only the event's own end, so a ring is a call that
//! stays answerable until the console says the event ended (but never for less than
//! `ring_min_hold`), or `ring_max_hold` passes with no end.
//!
//! Concurrency: Go's `emitMu` + `mu` become two `std` mutexes taken in the same order, and no
//! lock is held across an `.await`: every sink call is synchronous (the hub's sink only queues).
//! A timer is a task; a stale one is recognised by the call's serial, as Go compares the
//! `*ringCall`. The events frames are handled inline by the reader, so the only queue between
//! the network and the sink is the socket: nothing is buffered beyond one frame.

use crate::util::lock;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

use tokio::time::Instant;

use super::ws::{self, DialParams, PumpTiming};
use super::{Core, RingCall, EVENTS_PATH, KEY};
use crate::cancel::CancelToken;
use crate::driver::{
    DriverError, Sentinel, TalkbackSink, STATE_ACTIVE, STATE_ANSWERED, STATE_ENDED,
};
use crate::panic_hook::log_line;

/// `nextBackoff`: doubles `b` up to `max`.
pub fn next_backoff(b: Duration, max: Duration) -> Duration {
    let n = b.saturating_mul(2);
    if n > max {
        max
    } else {
        n
    }
}

/// The supervisor's restart wait after a panic: `wait` as it stands, or `min` again when the
/// watcher had run `stable_after` or more (a panic months after the last one is not a streak).
pub fn backoff_after_run(
    wait: Duration,
    ran: Duration,
    stable_after: Duration,
    min: Duration,
) -> Duration {
    if ran >= stable_after {
        min
    } else {
        wait
    }
}

/// The part of a public events frame this driver reads (`eventMsg`).
#[derive(Debug, Default, PartialEq, Eq)]
pub struct EventMsg {
    pub typ: String,
    pub id: String,
    pub model_key: String,
    pub item_type: String,
    pub device: String,
    /// the raw `end` value, if the key was present
    pub end: Option<String>,
}

impl EventMsg {
    /// `hasEnd`: the raw value is neither empty nor `null`.
    pub fn has_end(&self) -> bool {
        matches!(&self.end, Some(e) if !e.is_empty() && e != "null")
    }
}

/// The console's event ids are short opaque tokens (DEV-796 D10): an empty one, a long one or
/// one that carries anything else is ignored, since it would be stored and echoed as the
/// `call_id`.
fn valid_event_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 128
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b':'))
}

/// A JSON object key matched the way `encoding/json` matches a struct field: exact first, then
/// ASCII case folding.
fn field_index(names: &[&str], key: &str) -> Option<usize> {
    names
        .iter()
        .position(|n| *n == key)
        .or_else(|| names.iter().position(|n| n.eq_ignore_ascii_case(key)))
}

/// `json.Unmarshal(raw, &eventMsg)`: `None` is Go's error (a syntax error, or a field of the
/// wrong type anywhere, which makes `handle` ignore the frame). `null` is a zero message. A
/// repeated `item` object is merged into the first, as Go decodes into the same struct.
pub fn decode_event(raw: &[u8]) -> Option<EventMsg> {
    let text = crate::gostd::text::trim_json_space(std::str::from_utf8(raw).ok()?);
    if text == "null" {
        return Some(EventMsg::default());
    }
    let top = crate::ipc::parse_object_entries(text.as_bytes())?;
    let mut m = EventMsg::default();
    let mut bad = false;
    let string = |t: &str, bad: &mut bool, out: &mut String| {
        if t != "null" {
            match serde_json::from_str::<String>(t) {
                Ok(s) => *out = s,
                Err(_) => *bad = true,
            }
        }
    };
    for (key, val) in top {
        match field_index(&["type", "item"], &key) {
            Some(0) => string(val.get(), &mut bad, &mut m.typ),
            Some(1) => {
                let t = val.get();
                if t == "null" {
                    continue;
                }
                let Some(item) = crate::ipc::parse_object_entries(t.as_bytes()) else {
                    bad = true;
                    continue;
                };
                for (k, v) in item {
                    match field_index(&["id", "modelKey", "type", "device", "end"], &k) {
                        Some(0) => string(v.get(), &mut bad, &mut m.id),
                        Some(1) => string(v.get(), &mut bad, &mut m.model_key),
                        Some(2) => string(v.get(), &mut bad, &mut m.item_type),
                        Some(3) => string(v.get(), &mut bad, &mut m.device),
                        Some(4) => m.end = Some(v.get().to_string()),
                        _ => {}
                    }
                }
            }
            _ => {}
        }
    }
    (!bad).then_some(m)
}

async fn sleep_ctx(cancel: &CancelToken, d: Duration) -> bool {
    tokio::select! {
        _ = cancel.cancelled() => false,
        _ = tokio::time::sleep(d) => true,
    }
}

/// Runs the watcher under a supervisor: whatever happens to it, `done` is set when it is over
/// for good. A panic (Go's would end the process) is logged with its payload, reported as
/// unavailable, and the watcher is started again after a backoff (`backoff_min` doubling up to
/// `backoff_max`).
pub(super) fn spawn_watcher(
    core: Arc<Core>,
    parent: CancelToken,
    cancel: CancelToken,
    done: CancelToken,
) {
    // The hub's context ends the watcher too (Go: `context.WithCancel(ctx)`).
    let (p, c, d) = (parent, cancel.clone(), done.clone());
    tokio::spawn(async move {
        tokio::select! {
            _ = p.cancelled() => c.cancel(),
            _ = d.cancelled() => {}
        }
    });
    tokio::spawn(async move {
        // `done` is set however this task ends, so Stop never waits out its timeout for a
        // supervisor that died
        struct Done(CancelToken);
        impl Drop for Done {
            fn drop(&mut self) {
                self.0.cancel();
            }
        }
        let _done = Done(done);
        let t = core.timing;
        let mut wait = t.backoff_min;
        loop {
            let (c2, cancel2) = (core.clone(), cancel.clone());
            let started = Instant::now();
            let worker = tokio::spawn(async move {
                if c2.panic_in_watch.take() {
                    panic!("unifi_protect test panic in the watcher");
                }
                c2.watch(&cancel2).await
            });
            match worker.await {
                Ok(()) => break,
                // nothing aborts the watcher: not a panic means the run is over
                Err(je) if !je.is_panic() => {
                    log_line(&format!(
                        "portico-transport: {KEY}: the events watcher was cancelled"
                    ));
                    break;
                }
                Err(je) => {
                    let text = core.ptext(&*je.into_panic());
                    log_line(&format!(
                        "portico-transport: {KEY}: the events watcher panicked: {text}"
                    ));
                    if cancel.is_cancelled() {
                        break;
                    }
                    if catch_unwind(AssertUnwindSafe(|| core.report(false, "internal error")))
                        .is_err()
                    {
                        log_line(&format!(
                            "portico-transport: {KEY}: the sink panicked on an availability report"
                        ));
                    }
                    wait =
                        backoff_after_run(wait, started.elapsed(), t.stable_after, t.backoff_min);
                    if !sleep_ctx(&cancel, wait).await {
                        break;
                    }
                    wait = next_backoff(wait, t.backoff_max);
                }
            }
        }
    });
}

impl Core {
    /// `watch`: keeps the events websocket up until `cancel` fires: connect, read, and on any
    /// failure back off (bounded, doubling) and try again. Availability is reported on
    /// transitions only, and a connection that drops and comes straight back reports nothing.
    pub(super) async fn watch(self: &Arc<Self>, cancel: &CancelToken) {
        let t = self.timing;
        let mut backoff = t.backoff_min;
        let Some(api) = self.api() else { return };
        let tls = api.tls();
        while !cancel.is_cancelled() {
            let dialed = ws::dial(
                cancel,
                &DialParams {
                    scheme: &self.cfg.scheme,
                    host: &self.cfg.host_port,
                    path: EVENTS_PATH,
                    api_key: &self.cfg.key,
                    tls: &tls,
                    dial_timeout: t.dial_timeout,
                },
            )
            .await;
            let conn = match dialed {
                Ok(c) => c,
                Err(e) => {
                    if cancel.is_cancelled() {
                        return;
                    }
                    self.report(false, &ws::reason_for(&e));
                    if !sleep_ctx(cancel, backoff).await {
                        return;
                    }
                    backoff = next_backoff(backoff, t.backoff_max);
                    continue;
                }
            };
            self.report(true, "");
            let up = Instant::now();
            self.read(conn, cancel).await;
            if cancel.is_cancelled() {
                return;
            }
            if up.elapsed() >= t.stable_after {
                backoff = t.backoff_min;
            }
            if !sleep_ctx(cancel, backoff).await {
                return;
            }
            // Go evaluates this AFTER the sleep, so the sleep counts towards the connection's life.
            if up.elapsed() < t.stable_after {
                backoff = next_backoff(backoff, t.backoff_max);
            }
        }
    }

    /// `read`: pumps one connection until it fails or `cancel` fires, pinging on an interval.
    /// A frame whose handling panics is logged and skipped; the connection stays up.
    async fn read(self: &Arc<Self>, conn: ws::WsConn, cancel: &CancelToken) {
        let core = self.clone();
        let t = self.timing;
        let _ = ws::pump(
            conn,
            cancel,
            PumpTiming {
                ping_interval: t.ping_interval,
                pong_wait: t.pong_wait,
            },
            move |raw| {
                if let Err(p) = catch_unwind(AssertUnwindSafe(|| core.handle(raw))) {
                    log_line(&format!(
                        "portico-transport: {KEY}: panic handling an events frame: {}",
                        core.ptext(&*p)
                    ));
                }
            },
        )
        .await;
    }

    /// `handle`: reacts to one events frame. Anything that is not a ring of our camera is
    /// ignored, malformed frames included.
    pub(super) fn handle(self: &Arc<Self>, raw: &[u8]) {
        let Some(m) = decode_event(raw) else { return };
        if m.model_key != "event"
            || m.item_type != "ring"
            || m.id.is_empty()
            || m.device != self.cfg.camera_id
            || !valid_event_id(&m.id)
        {
            return;
        }
        match m.typ.as_str() {
            "add" => {
                self.on_ring(&m.id);
                if m.has_end() {
                    self.on_end(&m.id);
                }
            }
            "update" if m.has_end() => self.on_end(&m.id),
            _ => {}
        }
    }

    /// `report`: an availability transition, never a repeat.
    pub(super) fn report(&self, available: bool, reason: &str) {
        let mut a = lock(&self.avail);
        if a.0 && a.1 == available {
            return;
        }
        *a = (true, available);
        if let Some(sink) = self.sink() {
            sink.availability_changed(&self.cfg.device_id, available, reason);
        }
    }

    /// A panic payload as text, with the API key scrubbed (a payload can carry anything).
    pub(crate) fn ptext(&self, p: &(dyn std::any::Any + Send)) -> String {
        ws::scrub(
            &crate::util::panic_message(p, "<non-string payload>"),
            &self.cfg.key,
        )
    }

    fn arm(self: &Arc<Self>, serial: u64, wait: Duration) -> tokio::task::JoinHandle<()> {
        let core = self.clone();
        tokio::spawn(async move {
            tokio::time::sleep(wait).await;
            if let Err(p) = catch_unwind(AssertUnwindSafe(|| core.end_ringing(serial))) {
                log_line(&format!(
                    "portico-transport: {KEY}: panic ending a ring: {}",
                    core.ptext(&*p)
                ));
            }
        })
    }

    /// `onRing`: the ring edge: a new call, unless one is already live.
    pub(super) fn on_ring(self: &Arc<Self>, event_id: &str) {
        let _emit = lock(&self.emit);
        let mut st = lock(&self.st);
        if st.call.is_some() || st.events.is_none() || st.stopped {
            return;
        }
        let serial = self.serial.fetch_add(1, Ordering::SeqCst) + 1;
        let id = format!("unifi-{event_id}");
        let max = self.arm(serial, self.timing.ring_max_hold);
        st.call = Some(RingCall {
            serial,
            id: id.clone(),
            event_id: event_id.to_string(),
            started: Instant::now(),
            answering: false,
            answered: false,
            end_timer: None,
            max_timer: Some(max),
            talkback: None,
        });
        let sink = st.events.clone();
        drop(st);
        if let Some(sink) = sink {
            sink.ringing(&id, &self.cfg.device_id);
        }
    }

    /// `onEnd`: the console says the ring event ended. The call ends once `ring_min_hold` has
    /// passed since the ring.
    pub(super) fn on_end(self: &Arc<Self>, event_id: &str) {
        let mut st = lock(&self.st);
        let Some(c) = st.call.as_mut() else { return };
        if c.event_id != event_id || c.answered || c.answering || c.end_timer.is_some() {
            return;
        }
        let wait = self
            .timing
            .ring_min_hold
            .saturating_sub(c.started.elapsed());
        let serial = c.serial;
        c.end_timer = Some(self.arm(serial, wait));
    }

    /// `endRinging`: ends the call `serial` if nobody has answered it by now.
    pub(crate) fn end_ringing(self: &Arc<Self>, serial: u64) {
        let _emit = lock(&self.emit);
        let st = lock(&self.st);
        match st.call.as_ref() {
            Some(c) if c.serial == serial && !c.answered && !c.answering => {}
            _ => return,
        }
        self.end_locked(st);
    }

    /// `endLocked`: clears the call and reports it ended. Called with `emit` and `st` held;
    /// releases `st` (not `emit`) before reporting. Returns the call.
    fn end_locked(&self, mut st: std::sync::MutexGuard<'_, super::State>) -> Option<RingCall> {
        let c = st.call.take()?;
        c.stop_timers();
        let sink = st.events.clone();
        let stopped = st.stopped;
        drop(st);
        if let Some(sink) = sink {
            if !stopped {
                sink.state_changed(&c.id, STATE_ENDED);
            }
        }
        Some(c)
    }

    /// `RingingCall`: is `call_id` a ring still waiting for a client?
    pub(super) fn ringing_call(&self, call_id: &str) -> bool {
        let st = lock(&self.st);
        st.call
            .as_ref()
            .is_some_and(|c| c.id == call_id && !c.answered && !c.answering)
    }

    /// `AnswerTalkback`. A failed open leaves the call ringing. A future dropped while the
    /// session is opening gives the ring back (Go's cancelled context does the same).
    pub(super) async fn answer_talkback(
        self: &Arc<Self>,
        ctx: &CancelToken,
        call_id: &str,
    ) -> Result<Arc<dyn TalkbackSink>, DriverError> {
        let (serial, run) = {
            let mut st = lock(&self.st);
            let run = st.run;
            match st.call.as_mut() {
                Some(c) if c.id == call_id => {
                    if c.answered || c.answering {
                        return Err(DriverError::wrapping(
                            Sentinel::AlreadyAnswered,
                            format!(
                                "unifi_protect: {call_id} is already answered: already_answered"
                            ),
                        ));
                    }
                    c.answering = true;
                    (c.serial, run)
                }
                _ => {
                    return Err(DriverError::wrapping(
                        Sentinel::NoSuchCall,
                        format!("unifi_protect: {call_id} is not a live call: no_such_call"),
                    ))
                }
            }
        };
        let mut guard = Answering {
            core: self.clone(),
            serial,
            armed: true,
        };
        let opened = self.open_talkback(ctx).await;
        guard.armed = false;
        let t = match opened {
            Ok(t) => t,
            Err(e) => {
                self.release_answering(serial);
                let ended = {
                    let st = lock(&self.st);
                    st.stopped || st.run != run
                };
                if ended {
                    // the run ended while the session opened: the same answer as a hang-up
                    return Err(DriverError::wrapping(
                        Sentinel::NoSuchCall,
                        format!("unifi_protect: {call_id} ended while answering: no_such_call"),
                    ));
                }
                return Err(e);
            }
        };

        let emit = lock(&self.emit);
        let mut st = lock(&self.st);
        let live = st.call.as_ref().is_some_and(|c| c.serial == serial) && !st.stopped;
        if !live {
            // the driver was stopped, or the call hung up, while the session opened
            drop(st);
            drop(emit);
            let _ = t.sink.close();
            let mut st = lock(&self.st);
            if st.talkback.as_ref().is_some_and(|x| Arc::ptr_eq(x, &t)) {
                st.talkback = None;
            }
            return Err(DriverError::wrapping(
                Sentinel::NoSuchCall,
                format!("unifi_protect: {call_id} ended while answering: no_such_call"),
            ));
        }
        let (id, sink) = {
            let c = st.call.as_mut().expect("checked live");
            c.answering = false;
            c.answered = true;
            c.talkback = Some(t.clone());
            c.stop_timers();
            (c.id.clone(), st.events.clone())
        };
        drop(st);
        if let Some(sink) = sink {
            sink.state_changed(&id, STATE_ANSWERED);
            sink.state_changed(&id, STATE_ACTIVE);
        }
        drop(emit);

        // the session ending on its own ends the call
        let (core, t2) = (self.clone(), t.clone());
        tokio::spawn(async move {
            t2.done.cancelled().await;
            if let Err(p) = catch_unwind(AssertUnwindSafe(|| core.end_answered(serial))) {
                log_line(&format!(
                    "portico-transport: {KEY}: panic ending an answered call: {}",
                    core.ptext(&*p)
                ));
            }
        });
        Ok(t.sink.clone())
    }

    /// A failed or abandoned answer gives the ring back. The ring's timers may have fired WHILE
    /// it was `answering` (and returned without ending it); if one of them is spent, nothing is
    /// left to end the call, so it ends now (on a task: this runs from a `Drop`). Go leaves such
    /// a call ringing for ever (PARITY.md, Go defects).
    pub(super) fn release_answering(self: &Arc<Self>, serial: u64) {
        let spent = {
            let mut st = lock(&self.st);
            match st.call.as_mut() {
                Some(c) if c.serial == serial && !c.answered => {
                    c.answering = false;
                    c.max_timer.as_ref().is_some_and(|h| h.is_finished())
                        || c.end_timer.as_ref().is_some_and(|h| h.is_finished())
                }
                _ => false,
            }
        };
        if spent {
            if let Ok(h) = tokio::runtime::Handle::try_current() {
                let core = self.clone();
                h.spawn(async move {
                    if let Err(p) = catch_unwind(AssertUnwindSafe(|| core.end_ringing(serial))) {
                        log_line(&format!(
                            "portico-transport: {KEY}: panic ending a ring: {}",
                            core.ptext(&*p)
                        ));
                    }
                });
            }
        }
    }

    /// `endAnswered`: ends the answered call once its talkback is gone.
    fn end_answered(self: &Arc<Self>, serial: u64) {
        let _emit = lock(&self.emit);
        let st = lock(&self.st);
        if !st.call.as_ref().is_some_and(|c| c.serial == serial) {
            return;
        }
        self.end_locked(st);
    }

    /// `HangupCall`: ends `call_id`, ringing or answered, and closes its talkback.
    pub(super) fn hangup_call(self: &Arc<Self>, call_id: &str) -> Result<(), DriverError> {
        let emit = lock(&self.emit);
        let st = lock(&self.st);
        if !st.call.as_ref().is_some_and(|c| c.id == call_id) {
            drop(st);
            drop(emit);
            return Err(DriverError::wrapping(
                Sentinel::NoSuchCall,
                format!("unifi_protect: {call_id}: no_such_call"),
            ));
        }
        let c = self.end_locked(st);
        drop(emit);
        if let Some(t) = c.and_then(|c| c.talkback) {
            let _ = t.sink.close();
            // free the slot now, not when the watcher task gets to it
            let mut st = lock(&self.st);
            if st.talkback.as_ref().is_some_and(|x| Arc::ptr_eq(x, &t)) {
                st.talkback = None;
            }
        }
        Ok(())
    }
}

/// Gives the ring back if the `answer_talkback` future is dropped while the session opens.
struct Answering {
    core: Arc<Core>,
    serial: u64,
    armed: bool,
}

impl Drop for Answering {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        self.core.release_answering(self.serial);
    }
}
