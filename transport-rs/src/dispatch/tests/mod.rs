//! The dispatcher's unit tests, by content: `handlers` (the device verbs and `configure`),
//! `sink` (the event sink and its wrappers), `lanes` (dispatch classes and shutdown) and `wire`
//! (over a real socket). They need `Hub::detached`, `DeviceSink` and `put_if_absent`, which is
//! why they live in the crate; the Go name of a port is in a comment on each test.

use super::*;
use crate::driver::{
    BoxFuture, CallDialer, CallHangupper, DriverError, ExternalRinger, StreamSourcer,
};
use serde_json::value::RawValue;
use std::sync::atomic::{AtomicU32, Ordering};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;

mod handlers;
mod lanes;
mod sink;
mod wire;

/// (Go's tests also pass text that is not JSON; the IPC layer never hands a handler such a
/// line, so a JSON value of the wrong shape stands for it.)
fn p(json: &str) -> Params {
    Some(RawValue::from_string(json.to_string()).unwrap())
}

fn val(r: HandlerResult) -> Value {
    match r.expect("handler failed") {
        Reply::Json(v) => v,
        Reply::Raw(s) => serde_json::from_str(&s).unwrap(),
    }
}

fn code(r: HandlerResult) -> String {
    r.expect_err("handler succeeded").code
}

/// Next queued notification as (method, params).
fn note(h: &Hub) -> Option<(String, Value)> {
    h.events.pop_note().map(|n| match n {
        Note::Map(m, v) => (m.to_string(), v),
        Note::Raw(m, s) => (m.to_string(), serde_json::from_str(&s).unwrap()),
    })
}

fn notes(h: &Hub) -> Vec<(String, Value)> {
    std::iter::from_fn(|| note(h)).collect()
}

#[derive(Default)]
struct Fake {
    id: String,
    hangup: Mutex<Option<Result<(), DriverError>>>,
    hangups: Mutex<Vec<String>>,
    /// `Some`: the driver is a CallDialer and returns this
    dialer: Mutex<Option<Result<(), DriverError>>>,
    dialled: Mutex<Vec<String>>,
    legacy_dial: Mutex<Option<DriverError>>,
    legacy_dials: AtomicU32,
    answer_err: Mutex<Option<DriverError>>,
    ring: Mutex<Option<Result<(), DriverError>>>,
    stream: Mutex<Option<Result<String, DriverError>>>,
    stops: AtomicU32,
    sink: Mutex<Option<Arc<dyn EventSink>>>,
    /// R1-1: how long `stop` takes, and whether it has finished
    stop_delay: Mutex<Duration>,
    stopped: std::sync::atomic::AtomicBool,
    /// R1-2: states `dial_call` reports (through the sink) before it returns
    emit_in_dial: Mutex<Vec<&'static str>>,
    /// R2-83: a call `stop` reports as ended (through the sink) while it stops
    end_on_stop: Mutex<Option<String>>,
}

impl Fake {
    fn new(id: &str) -> Arc<Fake> {
        Arc::new(Fake {
            id: id.to_string(),
            ..Fake::default()
        })
    }
    fn sink(&self) -> Arc<dyn EventSink> {
        self.sink.lock().unwrap().clone().expect("not started")
    }
}

impl Driver for Fake {
    fn configure(&mut self, _: &serde_json::Map<String, Value>) -> Result<(), DriverError> {
        Ok(())
    }
    fn capabilities(&self) -> Capabilities {
        Capabilities::default()
    }
    fn start<'a>(
        &'a self,
        _: &'a CancelToken,
        events: Arc<dyn EventSink>,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        *self.sink.lock().unwrap() = Some(events);
        Box::pin(async { Ok(()) })
    }
    fn stop(&self) -> BoxFuture<'_, Result<(), DriverError>> {
        self.stops.fetch_add(1, Ordering::SeqCst);
        let delay = *self.stop_delay.lock().unwrap();
        if let Some(call) = self.end_on_stop.lock().unwrap().clone() {
            self.sink().state_changed(&call, "ended");
        }
        Box::pin(async move {
            tokio::time::sleep(delay).await;
            self.stopped.store(true, Ordering::SeqCst);
            Ok(())
        })
    }
    fn dial<'a>(
        &'a self,
        _: &'a CancelToken,
        _: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        self.legacy_dials.fetch_add(1, Ordering::SeqCst);
        let r = self.legacy_dial.lock().unwrap().clone();
        Box::pin(async move { r.map_or(Ok(()), Err) })
    }
    fn answer<'a>(
        &'a self,
        _: &'a CancelToken,
        _: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        let r = self.answer_err.lock().unwrap().clone();
        Box::pin(async move { r.map_or(Ok(()), Err) })
    }
    fn open_relay<'a>(&'a self, _: &'a CancelToken) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async { Ok(()) })
    }
    fn device_id(&self) -> Option<String> {
        Some(self.id.clone())
    }
    fn as_call_hangupper(&self) -> Option<&dyn CallHangupper> {
        self.hangup.lock().unwrap().is_some().then_some(self as _)
    }
    fn as_call_dialer(&self) -> Option<&dyn CallDialer> {
        self.dialer.lock().unwrap().is_some().then_some(self as _)
    }
    fn as_external_ringer(&self) -> Option<&dyn ExternalRinger> {
        self.ring.lock().unwrap().is_some().then_some(self as _)
    }
    fn as_stream_sourcer(&self) -> Option<&dyn StreamSourcer> {
        self.stream.lock().unwrap().is_some().then_some(self as _)
    }
}

impl CallHangupper for Fake {
    fn hangup_call(&self, call_id: &str) -> Result<(), DriverError> {
        self.hangups.lock().unwrap().push(call_id.to_string());
        // a door that placed calls holds only those (before any call it answers for every id)
        let dialled = self.dialled.lock().unwrap();
        if !dialled.is_empty() && !dialled.iter().any(|c| c == call_id) {
            return Err(DriverError::wrapping(Sentinel::NoSuchCall, "not mine"));
        }
        self.hangup.lock().unwrap().clone().unwrap()
    }
}

impl CallDialer for Fake {
    fn dial_call<'a>(
        &'a self,
        _: &'a CancelToken,
        call_id: &'a str,
        _: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        self.dialled.lock().unwrap().push(call_id.to_string());
        for st in self.emit_in_dial.lock().unwrap().iter() {
            self.sink().state_changed(call_id, st);
        }
        let r = self.dialer.lock().unwrap().clone().unwrap();
        Box::pin(async move { r })
    }
}

impl ExternalRinger for Fake {
    fn external_ring(&self, _: &str, _: Duration) -> Result<(), DriverError> {
        self.ring.lock().unwrap().clone().unwrap()
    }
}

impl StreamSourcer for Fake {
    fn stream_url<'a>(&'a self, _: &'a CancelToken) -> BoxFuture<'a, Result<String, DriverError>> {
        let r = self.stream.lock().unwrap().clone().unwrap();
        Box::pin(async move { r })
    }
}

/// What `configureHandler` does with a started driver: scoped sink, then the registry.
async fn add(h: &Arc<Hub>, f: &Arc<Fake>) {
    let sink = Arc::new(DeviceSink {
        s: h.events.clone(),
        device_id: f.id.clone(),
    });
    f.start(&h.ctx, sink).await.unwrap();
    assert!(h.put_if_absent(&f.id, f.clone()));
}

/// A transport on a temp socket with the dispatcher registered, and one connection to it.
pub(super) struct Rig {
    pub(super) hub: Arc<Hub>,
    pub(super) srv: Server,
    rd: BufReader<tokio::net::unix::OwnedReadHalf>,
    wr: tokio::net::unix::OwnedWriteHalf,
    dir: std::path::PathBuf,
    next: i64,
}

impl Rig {
    pub(super) async fn new(tag: &str) -> Rig {
        Self::build(tag, None).await
    }

    /// A rig whose notification writer takes `delay` over each note (R2-13): the notes are NOT
    /// on the wire by the time a handler returns, so only the reply barrier can put them
    /// before the reply.
    pub(super) async fn slow_notes(tag: &str, delay: Duration) -> Rig {
        Self::build(tag, Some(delay)).await
    }

    async fn build(tag: &str, delay: Option<Duration>) -> Rig {
        let dir = std::env::temp_dir().join(format!("disp-{}-{tag}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let sock = dir.join("t.sock");
        let srv = Server::listen(&sock).unwrap();
        let hub = match delay {
            None => register(&srv, HubOptions::default()),
            Some(d) => {
                let s = srv.clone();
                let events = IpcEventSink::with_writer(Arc::new(move |n| {
                    let s = s.clone();
                    Box::pin(async move {
                        tokio::time::sleep(d).await;
                        let _ = match n {
                            Note::Map(m, v) => s.notify(m, &v).await,
                            Note::Raw(m, raw) => s.notify_raw(m, &raw).await,
                        };
                    })
                }));
                register_on(&srv, HubOptions::default(), events)
            }
        };
        let s = srv.clone();
        tokio::spawn(async move { s.serve().await });
        let c = UnixStream::connect(&sock).await.unwrap();
        let (rd, wr) = c.into_split();
        Rig {
            hub,
            srv,
            rd: BufReader::new(rd),
            wr,
            dir,
            next: 0,
        }
    }

    /// Writes one request and returns at once (several can be in flight).
    async fn send(&mut self, id: i64, method: &str, params: &str) {
        let req = format!("{{\"id\":{id},\"method\":\"{method}\",\"params\":{params}}}\n");
        self.wr.write_all(req.as_bytes()).await.unwrap();
    }

    /// Reads lines until `n` replies (lines with an id) have arrived: (id -> line), and the
    /// notifications seen on the way, in order.
    async fn replies(&mut self, n: usize) -> (Vec<i64>, HashMap<i64, Value>, Vec<Value>) {
        let (mut ids, mut by_id, mut notes) = (Vec::new(), HashMap::new(), Vec::new());
        while ids.len() < n {
            let m = self.line().await;
            match m["id"].as_i64() {
                Some(id) => {
                    ids.push(id);
                    by_id.insert(id, m);
                }
                None => notes.push(m),
            }
        }
        (ids, by_id, notes)
    }

    async fn line(&mut self) -> Value {
        let mut l = String::new();
        tokio::time::timeout(Duration::from_secs(5), self.rd.read_line(&mut l))
            .await
            .expect("no line")
            .unwrap();
        serde_json::from_str(&l).unwrap_or_else(|_| panic!("not json: {l:?}"))
    }

    /// Sends a request and returns (raw text of the answer, parsed). Notifications that
    /// arrive first are returned in `notes`.
    pub(super) async fn call(&mut self, method: &str, params: Value) -> (String, Vec<Value>) {
        self.next += 1;
        let id = self.next;
        let req = format!("{{\"id\":{id},\"method\":\"{method}\",\"params\":{params}}}\n");
        self.wr.write_all(req.as_bytes()).await.unwrap();
        let mut notes = Vec::new();
        loop {
            let mut l = String::new();
            tokio::time::timeout(Duration::from_secs(5), self.rd.read_line(&mut l))
                .await
                .expect("no answer")
                .unwrap();
            if l.starts_with(&format!("{{\"id\":{id},")) {
                return (l.trim_end().to_string(), notes);
            }
            notes.push(serde_json::from_str(&l).unwrap());
        }
    }
}

impl Drop for Rig {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn server_cfg(extra: Value) -> Value {
    let mut m = json!({"device_id": "gate-sip-server:test", "sip_listen": "127.0.0.1:0",
        "rtp_port": 0, "password": "12345678"});
    for (k, v) in extra.as_object().cloned().unwrap_or_default() {
        m[k] = v;
    }
    json!({"driver": "gate_sip_server", "config": m})
}
