//! The event sink: one queued notification per driver event, written to the IPC connection in order (`ipcEventSink`, `deviceSink`).

use super::*;
use crate::gojson::Field;

// ── the event sink ───────────────────────────────────────────────────────────────────────────

/// One queued notification. Struct-shaped params keep Go's field order, so they travel as text.
#[derive(Debug)]
pub enum Note {
    Map(&'static str, Value),
    Raw(&'static str, String),
}

impl Note {
    /// A ring and the end of a call are never evicted from a full queue (see `evqueue`).
    pub(super) fn is_critical(&self) -> bool {
        match self {
            Note::Map("ringing", _) => true,
            Note::Map("call_state_changed", v) => v["state"] == STATE_ENDED,
            _ => false,
        }
    }
}

/// Notifications waiting for the connection: the queue is bounded at `NOTE_QUEUE_MAX`; a driver
/// reports from its own task and never waits for Python (Go's `Notify` blocks the reporting
/// goroutine, up to the 5 s write timeout). When Python is slow the oldest low-value
/// notification is dropped, never a ring or the end of a call, and every drop is logged.
pub(super) const NOTE_QUEUE_MAX: usize = 1024;
/// Rings and call ends stop being queued only at this many.
pub(super) const NOTE_QUEUE_HARD: usize = 4096;

pub(super) struct PendingDial {
    pub(super) device_id: String,
    pub(super) at: std::time::Instant,
}

/// How long a dialled device waits to claim the call id of its first transition.
pub(super) const DIAL_CLAIM_WINDOW: Duration = Duration::from_secs(30);

#[derive(Default)]
pub(super) struct SinkState {
    /// call_id -> device_id
    pub(super) calls: HashMap<String, String>,
    /// dials whose call id has not been seen yet, oldest first
    pub(super) dials: Vec<PendingDial>,
}

/// `ipcEventSink`: forwards a driver's events to Python as notifications. Drivers call it from
/// their own tasks; it holds the `call_id -> device_id` map that stamps `device_id` on
/// `call_state_changed`.
pub struct IpcEventSink {
    pub(super) queue: EvQueue<Note>,
    pub(super) sig: Notify,
    pub(super) st: Mutex<SinkState>,
    /// notes handed to the queue / notes the writer is done with or that were dropped (R1-2,
    /// R1-16: [`IpcEventSink::flush`] waits for `done` to reach what `pushed` was)
    pub(super) pushed: AtomicU64,
    pub(super) done: tokio::sync::watch::Sender<u64>,
    pub(super) has_writer: AtomicBool,
    /// `outSeq`: the id source if the OS random source ever fails (R2-111)
    out_seq: AtomicU64,
}

/// What writes one note to the socket (the sink's writer task calls it, in order).
pub(super) type NoteWriter =
    Arc<dyn Fn(Note) -> Pin<Box<dyn Future<Output = ()> + Send>> + Send + Sync>;

impl IpcEventSink {
    /// Starts the task that writes the notifications, in order, to `srv`. Needs a runtime.
    pub fn new(srv: Server) -> Arc<IpcEventSink> {
        Self::with_writer(Arc::new(move |n| {
            let srv = srv.clone();
            Box::pin(async move {
                // Go ignores the error too ("ipc: no connection" before Python connects).
                let _ = match n {
                    Note::Map(m, v) => srv.notify(m, &v).await,
                    Note::Raw(m, raw) => srv.notify_raw(m, &raw).await,
                };
            })
        }))
    }

    /// [`IpcEventSink::new`] with the write abstracted (the writer task is supervised: a panic
    /// in `write` is logged and the writer starts again, R1-16).
    pub(super) fn with_writer(write: NoteWriter) -> Arc<IpcEventSink> {
        let sink = Self::detached();
        sink.has_writer.store(true, Ordering::SeqCst);
        let s = sink.clone();
        tokio::spawn(async move {
            loop {
                let writer = tokio::spawn(Self::write_loop(s.clone(), write.clone()));
                match writer.await {
                    // `write_loop` never returns; if a change makes it, the supervisor ends
                    // instead of spinning
                    Ok(()) => return,
                    Err(_) => {
                        // the note being written is lost; every later one is still delivered
                        crate::panic_hook::err_line(
                            "portico-transport: the notification writer panicked and was restarted",
                        );
                        s.finish_one();
                    }
                }
            }
        });
        sink
    }

    pub(super) async fn write_loop(s: Arc<IpcEventSink>, write: NoteWriter) {
        loop {
            let Some(n) = s.queue.pop() else {
                s.sig.notified().await;
                continue;
            };
            write(n).await;
            s.finish_one();
        }
    }

    pub(super) fn finish_one(&self) {
        self.done.send_modify(|d| *d += 1);
    }

    /// Waits (at most `limit`) until every note queued before this call has been written or
    /// dropped. A sink with no writer (tests) returns at once.
    pub async fn flush(&self, limit: Duration) {
        if !self.has_writer.load(Ordering::SeqCst) {
            return;
        }
        let target = self.pushed.load(Ordering::SeqCst);
        let mut rx = self.done.subscribe();
        let _ = tokio::time::timeout(limit, rx.wait_for(|d| *d >= target)).await;
    }

    /// A sink with no writer: the notifications stay queued (tests read them with `pop_note`).
    pub fn detached() -> Arc<IpcEventSink> {
        Arc::new(IpcEventSink {
            queue: EvQueue::new(NOTE_QUEUE_MAX, NOTE_QUEUE_HARD, Note::is_critical),
            sig: Notify::new(),
            st: Mutex::new(SinkState::default()),
            pushed: AtomicU64::new(0),
            done: tokio::sync::watch::channel(0).0,
            has_writer: AtomicBool::new(false),
            out_seq: AtomicU64::new(0),
        })
    }

    /// The next queued notification (the writer's side; tests use it directly).
    pub fn pop_note(&self) -> Option<Note> {
        self.queue.pop()
    }

    /// How many notifications were dropped because the queue was full.
    pub fn dropped_notes(&self) -> u64 {
        self.queue.dropped()
    }

    pub(super) fn note(&self, n: Note) {
        self.pushed.fetch_add(1, Ordering::SeqCst);
        let (d, total) = self.queue.push(n);
        if d != Dropped::Nothing {
            self.finish_one(); // one note will never be written
            let what = match d {
                Dropped::OldestLowValue => "the oldest low-value notification",
                _ => "the new notification",
            };
            // the first drop and then every 100th: a burst of thousands is one line, not thousands
            if total == 1 || total % 100 == 0 {
                crate::panic_hook::err_line(&format!(
                    "portico-transport: notification queue full, {what} was dropped ({total} dropped so far)"
                ));
            }
        }
        self.sig.notify_one();
    }

    pub(super) fn st(&self) -> std::sync::MutexGuard<'_, SinkState> {
        lock(&self.st)
    }

    /// `Ringing`.
    pub fn ringing(&self, call_id: &str, device_id: &str) {
        self.st()
            .calls
            .insert(call_id.to_string(), device_id.to_string());
        crate::panic_hook::log_line(&format!(
            "portico-transport: hub emits ringing call={call_id} device={device_id}"
        ));
        self.note(Note::Map(
            "ringing",
            json!({"call_id": call_id, "device_id": device_id}),
        ));
    }

    /// `stateChanged`: a transition reported by `reporter` (`""` when the reporting device is
    /// not known).
    pub fn state_changed(&self, call_id: &str, reporter: &str, state: &str) {
        let device_id = {
            let mut g = self.st();
            let mut known = g.calls.get(call_id).cloned();
            if known.is_none() {
                known = Self::claim_dial_locked(&mut g, call_id, reporter);
            }
            if known.is_some() && state == STATE_ENDED {
                g.calls.remove(call_id);
            }
            known.unwrap_or_default()
        };
        crate::panic_hook::log_line(&format!(
            "portico-transport: hub emits call_state_changed call={call_id} device={} state={state}",
            if device_id.is_empty() { "-" } else { &device_id }
        ));
        self.note(Note::Map(
            "call_state_changed",
            json!({"call_id": call_id, "device_id": device_id, "state": state}),
        ));
    }

    /// `claimDialLocked`: gives an unannounced call to the oldest dial of `reporter` still
    /// waiting for one, and to no other device's dial. Dials past the window are dropped.
    pub(super) fn claim_dial_locked(
        g: &mut SinkState,
        call_id: &str,
        reporter: &str,
    ) -> Option<String> {
        let now = std::time::Instant::now();
        g.dials
            .retain(|d| now.duration_since(d.at) <= DIAL_CLAIM_WINDOW);
        if reporter.is_empty() {
            return None;
        }
        let i = g.dials.iter().position(|d| d.device_id == reporter)?;
        let d = g.dials.remove(i);
        g.calls.insert(call_id.to_string(), d.device_id.clone());
        Some(d.device_id)
    }

    /// DEV-811 P3: `call_audio_stats {device_id, call_id, ...counters}`, once per answered call
    /// at the end of its audio. Only a driver configured with `call_stats` ever calls it. Low
    /// value: dropped before a ring or the end of a call when the queue is full.
    pub fn call_audio_stats(&self, device_id: &str, call_id: &str, stats: &Value) {
        let mut params = json!({"device_id": device_id, "call_id": call_id});
        if let (Some(p), Some(s)) = (params.as_object_mut(), stats.as_object()) {
            for (k, v) in s {
                p.insert(k.clone(), v.clone());
            }
        }
        self.note(Note::Map("call_audio_stats", params));
    }

    /// `DriverError` (the `errorReporter` extension): `{code, device_id, message}`, never a
    /// credential. Reachable from a driver through [`EventSink::error_reporter`] (slice 6: the
    /// registration failure of dahua_sip).
    pub fn driver_error(&self, device_id: &str, code: &str, msg: &str) {
        self.note(Note::Map(
            "driver_error",
            json!({"device_id": device_id, "message": msg, "code": code}),
        ));
    }

    /// `AvailabilityChanged`: `{device_id, available, reason}` in struct order.
    pub fn availability_changed(&self, device_id: &str, available: bool, reason: &str) {
        let reason = if available { "" } else { reason };
        self.note(Note::Raw(
            "driver_availability",
            gojson::object(&[
                ("device_id", Field::Str(device_id)),
                ("available", Field::Bool(available)),
                ("reason", Field::Str(reason)),
            ]),
        ));
    }

    /// `ListenStateChanged`.
    pub fn listen_state_changed(&self, device_id: &str, state: &str, reason: &str) {
        self.note(Note::Raw(
            "listen_state",
            gojson::object(&[
                ("device_id", Field::Str(device_id)),
                ("state", Field::Str(state)),
                ("reason", Field::Str(reason)),
            ]),
        ));
    }

    /// `CallReason`.
    pub fn call_reason(&self, device_id: &str, call_id: &str, kind: &str, reason: &str) {
        self.note(Note::Raw(
            "call_reason",
            gojson::object(&[
                ("device_id", Field::Str(device_id)),
                ("call_id", Field::Str(call_id)),
                ("kind", Field::Str(kind)),
                ("reason", Field::Str(reason)),
            ]),
        ));
    }

    /// `CapabilitiesChanged`: the whole capability set again, in the shape `configure` returned.
    pub fn capabilities_changed(&self, device_id: &str, caps: &Capabilities) {
        self.note(Note::Raw(
            "capabilities_changed",
            gojson::object(&[
                ("device_id", Field::Str(device_id)),
                ("capabilities", Field::Raw(&caps.to_wire())),
            ]),
        ));
    }

    /// `newOutboundCall`: mints a call id for a dial of `device_id` that a `CallDialer` is about
    /// to place, and remembers it. Announces nothing.
    pub fn new_outbound_call(&self, device_id: &str) -> String {
        let mut g = self.st();
        loop {
            let mut b = [0u8; 8];
            if getrandom::getrandom(&mut b).is_err() {
                // crypto/rand does not fail on supported platforms; a counter keeps the id
                // unique if it ever does (Go, main.go:277-282), and nothing panics under the lock
                let n = self.out_seq.fetch_add(1, Ordering::SeqCst) + 1;
                b = n.to_be_bytes();
            }
            let id = format!("out-{}", crate::util::hex(&b));
            if !g.calls.contains_key(&id) {
                g.calls.insert(id.clone(), device_id.to_string());
                return id;
            }
        }
    }

    /// `forgetCall`: drops one call the sink remembers (its dial failed).
    pub fn forget_call(&self, call_id: &str) {
        self.st().calls.remove(call_id);
    }

    /// `expectDial`: registers a dial of `device_id` about to be handed to its driver.
    pub fn expect_dial(&self, device_id: &str) {
        self.st().dials.push(PendingDial {
            device_id: device_id.to_string(),
            at: std::time::Instant::now(),
        });
    }

    /// `cancelDial`: withdraws the newest waiting dial of `device_id` (its Dial failed).
    pub fn cancel_dial(&self, device_id: &str) {
        let mut g = self.st();
        if let Some(i) = g.dials.iter().rposition(|d| d.device_id == device_id) {
            g.dials.remove(i);
        }
    }

    /// `forgetDevice`: drops everything the sink remembers about `device_id`.
    pub fn forget_device(&self, device_id: &str) {
        let mut g = self.st();
        g.calls.retain(|_, d| d != device_id);
        g.dials.retain(|d| d.device_id != device_id);
    }

    /// `adopt` (external_ring.go): records that `call_id` belongs to `device_id` without
    /// announcing it.
    pub fn adopt(&self, call_id: &str, device_id: &str) {
        self.st()
            .calls
            .insert(call_id.to_string(), device_id.to_string());
    }
}

/// `deviceSink`: the sink as seen by one device's driver. It adds the device identity for the
/// pending-dial claim and forwards every optional extension (`ListenStateSink`,
/// `CapabilitiesSink`, `CallReasonSink`): a wrapper that forgot one would silence it.
pub(super) struct DeviceSink {
    pub(super) s: Arc<IpcEventSink>,
    pub(super) device_id: String,
}

impl EventSink for DeviceSink {
    fn ringing(&self, call_id: &str, device_id: &str) {
        self.s.ringing(call_id, device_id);
    }
    fn state_changed(&self, call_id: &str, state: &str) {
        self.s.state_changed(call_id, &self.device_id, state);
    }
    fn availability_changed(&self, device_id: &str, available: bool, reason: &str) {
        self.s.availability_changed(device_id, available, reason);
    }
    fn listen_state_sink(&self) -> Option<&dyn ListenStateSink> {
        Some(self)
    }
    fn capabilities_sink(&self) -> Option<&dyn CapabilitiesSink> {
        Some(self)
    }
    fn call_reason_sink(&self) -> Option<&dyn CallReasonSink> {
        Some(self)
    }
    fn error_reporter(&self) -> Option<&dyn ErrorReporter> {
        Some(self)
    }
    fn call_stats_sink(&self) -> Option<&dyn CallStatsSink> {
        Some(self)
    }
}

impl CallStatsSink for DeviceSink {
    fn call_audio_stats(&self, call_id: &str, stats: &Value) {
        self.s.call_audio_stats(&self.device_id, call_id, stats);
    }
}

impl ErrorReporter for DeviceSink {
    fn driver_error(&self, device_id: &str, code: &str, msg: &str) {
        self.s.driver_error(device_id, code, msg);
    }
}

impl ListenStateSink for DeviceSink {
    fn listen_state_changed(&self, device_id: &str, state: &str, reason: &str) {
        self.s.listen_state_changed(device_id, state, reason);
    }
}

impl CapabilitiesSink for DeviceSink {
    fn capabilities_changed(&self, device_id: &str, caps: &Capabilities) {
        self.s.capabilities_changed(device_id, caps);
    }
}

impl CallReasonSink for DeviceSink {
    fn call_reason(&self, device_id: &str, call_id: &str, kind: &str, reason: &str) {
        self.s.call_reason(device_id, call_id, kind, reason);
    }
}
