//! Port of `transport/internal/ipc/ipc.go` (design doc 9.4): a Unix domain socket carrying
//! newline-delimited JSON-RPC-shaped messages, and (slice 10) of `tcp.go`, the same protocol
//! over a token-authenticated TCP socket for the standalone container ([`Server::listen_tcp`]).
//!
//! Requests: `{id, method, params}`; responses `{id, result | error{code,message}}`;
//! notifications (Rust to Python, no response) `{method, params}`.
//!
//! Dispatch semantics, as in Go: a request's handler may block for seconds, so by default each
//! request runs on its own task and never holds up the read loop; responses are matched by id.
//! Two exceptions are explicit at registration:
//! * [`Server::handle_inline`] runs on the read loop itself (the `media_*` audio lane: frames of
//!   a call are delivered in the order Python wrote them);
//! * [`Server::handle_keyed`] runs off the loop but strictly one at a time, in arrival order,
//!   per key (webrtc_* per call_id, configure/deconfigure per device_id).
//!
//! The server accepts exactly one connection at a time.

use crate::util::lock;
use std::collections::HashMap;
use std::future::Future;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex as StdMutex, RwLock};
use std::time::Duration;

use serde::de::{Deserialize, Deserializer, MapAccess, Visitor};
use serde_json::value::RawValue;
use serde_json::Value;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::{TcpListener, UnixListener};
use tokio::sync::{oneshot, watch, Mutex};

use crate::gojson;

mod line;
mod tcp;
pub(crate) use line::{LineScanner, Scan};
use tcp::TcpGate;
pub use tcp::{Prefix, TcpOptions, AUTH_WINDOW, MIN_TOKEN_LEN};

/// Go's `bufio.Scanner` buffer ceiling in `Serve` (`scanner.Buffer(make([]byte, 4096), 1<<20)`).
/// A line plus its `\n` must fit in this many bytes.
pub const MAX_LINE: usize = 1 << 20;

/// What `bufio.ErrTooLong` prints.
pub const ERR_TOO_LONG: &str = "bufio.Scanner: token too long";

/// A request's `params` exactly as received (`None` = the field was absent; a literal `null`
/// is `Some("null")`, as Go's `json.RawMessage` keeps it).
pub type Params = Option<Box<RawValue>>;

/// The raw JSON text of `params`; `""` when absent (what a nil `json.RawMessage` looks like).
pub fn raw_str(p: &Params) -> &str {
    p.as_deref().map(RawValue::get).unwrap_or("")
}

/// An error answer: `{"code":..., "message":...}`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IpcError {
    pub code: String,
    pub message: String,
}

impl IpcError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        IpcError {
            code: code.into(),
            message: message.into(),
        }
    }
}

/// A successful answer. [`Reply::Json`] is marshalled with sorted keys (a Go `map`);
/// [`Reply::Raw`] is JSON text that is already in its final form and is written verbatim: the
/// result of a Go struct without tags (the `configure` reply is `driver.Capabilities`), whose
/// field order a sorted `Value` cannot reproduce.
#[derive(Debug, Clone, PartialEq)]
pub enum Reply {
    Json(Value),
    Raw(String),
}

impl From<Value> for Reply {
    fn from(v: Value) -> Reply {
        Reply::Json(v)
    }
}

pub type HandlerResult = Result<Reply, IpcError>;
pub type BoxFuture = Pin<Box<dyn Future<Output = HandlerResult> + Send>>;

/// Processes one request's params and returns a JSON value or an [`IpcError`]. Any
/// `Fn(Params) -> impl Future<Output = HandlerResult>` is a handler.
pub trait Handler: Send + Sync + 'static {
    fn call(&self, params: Params) -> BoxFuture;
}

impl<F, Fut> Handler for F
where
    F: Fn(Params) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = HandlerResult> + Send + 'static,
{
    fn call(&self, params: Params) -> BoxFuture {
        Box::pin(self(params))
    }
}

/// Derives a keyed route's serialisation key from the raw params; `""` means no lane.
pub type KeyFn = Arc<dyn Fn(&str) -> String + Send + Sync>;

#[derive(Clone)]
enum Dispatch {
    Plain,
    Inline,
    Keyed(KeyFn),
}

/// What [`Server::dispatch_class`] reports.
#[cfg(feature = "test-seams")]
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DispatchClass {
    /// its own task, no ordering
    Plain,
    /// on the read loop (the `media_*` lane)
    Inline,
    /// serialised per lane key (empty: no lane for these params)
    Keyed(String),
}

#[derive(Clone)]
struct Route {
    handler: Arc<dyn Handler>,
    dispatch: Dispatch,
}

type BoxRead = Box<dyn AsyncRead + Send + Unpin>;
type BoxWrite = Box<dyn AsyncWrite + Send + Unpin>;

struct Conn {
    id: u64,
    /// `None` once the connection has been closed (Go's `conn.Close()`).
    writer: Option<BoxWrite>,
    dead: watch::Sender<bool>,
}

/// What the server accepts on: the Unix socket HA's own subprocess uses, or the standalone
/// container's TCP port (`tcp.go`).
enum Listener {
    Unix {
        l: UnixListener,
        path: PathBuf,
    },
    /// The listener is taken out (and so closed) by [`Server::close`], as Go's
    /// `listener.Close()` frees the port at once; a `serve` blocked in `accept` holds its own
    /// handle and drops it when the close signal wakes it.
    Tcp {
        l: StdMutex<Option<Arc<TcpListener>>>,
        gate: Box<TcpGate>,
        local: SocketAddr,
    },
}

#[derive(Default)]
struct State {
    conn: Option<Conn>,
}

struct Inner {
    listener: Listener,
    /// "unix" or "tcp": the word in the texts of read and write errors (Go's `net.OpError`).
    net: &'static str,
    unlinked: AtomicBool,
    handlers: RwLock<HashMap<String, Route>>,
    lanes: StdMutex<HashMap<String, (u64, oneshot::Receiver<()>)>>,
    next_id: AtomicU64,
    state: Mutex<State>,
    closed: AtomicBool,
    close_tx: watch::Sender<bool>,
    write_timeout: StdMutex<Duration>,
    /// R1-2: awaited before every response is written, see [`Server::set_reply_barrier`]
    reply_barrier: StdMutex<Option<ReplyBarrier>>,
}

/// What [`Server::set_reply_barrier`] holds.
pub type ReplyBarrier = Arc<dyn Fn() -> Pin<Box<dyn Future<Output = ()> + Send>> + Send + Sync>;

/// The IPC server. Cloning gives another handle to the same server.
#[derive(Clone)]
pub struct Server(Arc<Inner>);

/// One decoded request line.
struct Request {
    id: i64,
    method: String,
    params: Params,
}

struct Fields(Vec<(String, Box<RawValue>)>);

impl<'de> Deserialize<'de> for Fields {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Fields;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a JSON object")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut m: A) -> Result<Fields, A::Error> {
                let mut v = Vec::new();
                while let Some((k, val)) = m.next_entry::<String, Box<RawValue>>()? {
                    v.push((k, val));
                }
                Ok(Fields(v))
            }
        }
        d.deserialize_map(V)
    }
}

/// The members of a JSON object, in order, duplicates kept. `None` when `bytes` is not one
/// valid JSON object.
pub(crate) fn parse_object_entries(bytes: &[u8]) -> Option<Vec<(String, Box<RawValue>)>> {
    serde_json::from_slice::<Fields>(bytes).ok().map(|f| f.0)
}

/// Mirrors `json.Unmarshal(line, &Request)`: `None` is Go's "malformed line, drop it".
///
/// Matches Go's quirks: a literal `null` is a zero Request; keys match case-insensitively
/// (exact match first); `id` must be an integer literal; any field of the wrong type drops the
/// whole line; for duplicate keys the last one wins.
fn parse_request(line: &[u8]) -> Option<Request> {
    let fields = match serde_json::from_slice::<Option<Fields>>(line) {
        Ok(Some(f)) => f.0,
        Ok(None) => Vec::new(),
        Err(_) => return None,
    };
    let mut req = Request {
        id: 0,
        method: String::new(),
        params: None,
    };
    for (key, val) in fields {
        let which = ["id", "method", "params"]
            .iter()
            .position(|n| *n == key)
            .or_else(|| {
                ["id", "method", "params"]
                    .iter()
                    .position(|n| n.eq_ignore_ascii_case(&key))
            });
        let text = val.get();
        match which {
            Some(0) => {
                if text != "null" {
                    req.id = text.parse::<i64>().ok()?;
                }
            }
            Some(1) => {
                if text != "null" {
                    req.method = serde_json::from_str::<String>(text).ok()?;
                }
            }
            Some(2) => req.params = Some(val),
            _ => {}
        }
    }
    Some(req)
}

fn response_line(id: i64, r: &HandlerResult) -> String {
    let mut s = format!("{{\"id\":{id}");
    match r {
        Ok(Reply::Json(v)) => {
            s.push_str(",\"result\":");
            s.push_str(&gojson::marshal(v));
        }
        Ok(Reply::Raw(text)) => {
            s.push_str(",\"result\":");
            s.push_str(text);
        }
        Err(e) => {
            s.push_str(",\"error\":{\"code\":");
            gojson::push_string(&mut s, &e.code);
            s.push_str(",\"message\":");
            gojson::push_string(&mut s, &e.message);
            s.push('}');
        }
    }
    s.push_str("}\n");
    s
}

fn notification_line(method: &str, params: &Value) -> String {
    let mut s = String::from("{\"method\":");
    gojson::push_string(&mut s, method);
    s.push_str(",\"params\":");
    s.push_str(&gojson::marshal(params));
    s.push_str("}\n");
    s
}

impl Server {
    /// Creates the Unix socket at `socket_path`, removing a stale one left behind by a previous
    /// crash first (the integration restarts the subprocess on crash; a leftover socket file
    /// would otherwise make the bind fail with "address already in use"). The socket is then
    /// restricted to mode 0600. Must be called inside a tokio runtime.
    pub fn listen(socket_path: impl AsRef<Path>) -> Result<Server, String> {
        let path = socket_path.as_ref();
        let shown = path.display();
        let _ = std::fs::remove_file(path);
        let listener = UnixListener::bind(path).map_err(|e| {
            format!(
                "ipc: listening on {shown}: listen unix {shown}: bind: {}",
                gojson::io_err(&e)
            )
        })?;
        // The socket's mode is otherwise 0777 &^ umask, and a connection is full control.
        // Owner-only; fail closed if it cannot be set.
        use std::os::unix::fs::PermissionsExt;
        if let Err(e) = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)) {
            drop(listener);
            let _ = std::fs::remove_file(path);
            return Err(format!(
                "ipc: restricting {shown} to 0600: chmod {shown}: {}",
                gojson::io_err(&e)
            ));
        }
        Ok(Server::new(Listener::Unix {
            l: listener,
            path: path.to_path_buf(),
        }))
    }

    fn new(listener: Listener) -> Server {
        let (close_tx, _) = watch::channel(false);
        Server(Arc::new(Inner {
            net: match listener {
                Listener::Unix { .. } => "unix",
                Listener::Tcp { .. } => "tcp",
            },
            listener,
            unlinked: AtomicBool::new(false),
            handlers: RwLock::new(HashMap::new()),
            lanes: StdMutex::new(HashMap::new()),
            next_id: AtomicU64::new(0),
            state: Mutex::new(State::default()),
            closed: AtomicBool::new(false),
            close_tx,
            write_timeout: StdMutex::new(Duration::from_secs(5)),
            reply_barrier: StdMutex::new(None),
        }))
    }

    fn register(&self, method: &str, handler: impl Handler, dispatch: Dispatch) {
        self.0.handlers.write().unwrap().insert(
            method.to_string(),
            Route {
                handler: Arc::new(handler),
                dispatch,
            },
        );
    }

    /// Registers `h` for `method`, run on its own task per request. Call before `serve`.
    pub fn handle(&self, method: &str, h: impl Handler) {
        self.register(method, h, Dispatch::Plain);
    }

    /// Registers `h` to run on the read loop. Use it only for a handler that never blocks.
    pub fn handle_inline(&self, method: &str, h: impl Handler) {
        self.register(method, h, Dispatch::Inline);
    }

    /// Registers `h` to run off the read loop, serialised in arrival order with every other
    /// keyed request that yields the same non-empty key.
    pub fn handle_keyed(&self, method: &str, key: KeyFn, h: impl Handler) {
        self.register(method, h, Dispatch::Keyed(key));
    }

    /// How `method` is dispatched, and for a keyed method its lane key for `params` (a JSON
    /// text; `None` params as the empty string). `None` for an unregistered method. Go pins
    /// the class of every registration with a regular expression over `main.go`; this is the
    /// same table, read from the live server (R2-1). A test seam.
    #[cfg(feature = "test-seams")]
    pub fn dispatch_class(&self, method: &str, params: &str) -> Option<DispatchClass> {
        let route = self.0.handlers.read().unwrap().get(method).cloned()?;
        Some(match route.dispatch {
            Dispatch::Plain => DispatchClass::Plain,
            Dispatch::Inline => DispatchClass::Inline,
            Dispatch::Keyed(f) => DispatchClass::Keyed(f(params)),
        })
    }

    /// The per-write deadline (Go: `writeTimeout`, 5 s). Python stops draining the socket when
    /// its loop is busy or restarting; without a deadline a write would block forever holding
    /// the write lock and every notifier behind it.
    #[cfg(feature = "test-seams")]
    pub fn set_write_timeout(&self, d: Duration) {
        *lock(&self.0.write_timeout) = d;
    }

    /// R1-2: `barrier` is awaited after a handler has returned and before its response is
    /// written. The hub uses it to put every notification a driver emitted INSIDE the handler on
    /// the wire first, as Go does (its `Notify` writes in the calling goroutine, so the event of
    /// `Start` or `DialCall` precedes the reply of `configure` / `dial`).
    pub fn set_reply_barrier(&self, barrier: ReplyBarrier) {
        *lock(&self.0.reply_barrier) = Some(barrier);
    }

    /// True once a connection has been accepted (Go tests peek at `srv.enc != nil`).
    pub async fn has_connection(&self) -> bool {
        self.0.state.lock().await.conn.is_some()
    }

    pub fn is_closed(&self) -> bool {
        self.0.closed.load(Ordering::SeqCst)
    }

    /// Blocks accepting a single connection and processing newline-delimited requests until it
    /// closes, returning `Ok` on a clean close. Call again to accept a reconnect.
    pub async fn serve(&self) -> Result<(), String> {
        let inner = &self.0;
        let mut close_rx = inner.close_tx.subscribe();
        if self.is_closed() {
            return Ok(()); // Accept on a closed listener fails and isClosed makes it an orderly stop
        }
        let mut buf: Vec<u8> = Vec::with_capacity(4096);
        let (rd, wr): (BoxRead, BoxWrite) = match &inner.listener {
            Listener::Unix { l, .. } => {
                let accepted = tokio::select! {
                    r = l.accept() => r,
                    _ = close_rx.wait_for(|c| *c) => return Ok(()),
                };
                match accepted {
                    Ok((s, _)) => {
                        let (r, w) = s.into_split();
                        (Box::new(r), Box::new(w))
                    }
                    Err(e) => return self.accept_failed(e),
                }
            }
            Listener::Tcp { gate, .. } => {
                // R1-37: accepted and checked on their own tasks; a connection that passed the
                // token check is never refused or delayed by one that has not (it is not even
                // served until it has proved it is the hub, and then it replaces the current one).
                self.ensure_acceptor();
                let a = tokio::select! {
                    a = gate.next_admitted() => a,
                    _ = close_rx.wait_for(|c| *c) => return Ok(()),
                };
                buf = a.buf;
                (a.rd, a.wr)
            }
        };
        let tcp_gate = match &inner.listener {
            Listener::Tcp { gate, .. } => Some(&**gate),
            Listener::Unix { .. } => None,
        };
        let (dead_tx, dead_rx) = watch::channel(false);
        let conn_id = self.next_id();
        {
            let mut st = inner.state.lock().await;
            if self.is_closed() {
                // Close raced the accept; it never saw this connection.
                return Ok(());
            }
            st.conn = Some(Conn {
                id: conn_id,
                writer: Some(wr),
                dead: dead_tx,
            });
        }

        let result = self
            .read_requests(conn_id, buf, rd, dead_rx, tcp_gate)
            .await;
        // defer conn.Close()
        {
            let mut st = inner.state.lock().await;
            if let Some(c) = st.conn.as_mut() {
                if c.id == conn_id {
                    c.writer = None;
                    c.dead.send_replace(true);
                }
            }
        }
        if self.is_closed() {
            return Ok(()); // our own Close ended the read
        }
        result
    }

    /// The read half of one connection: every complete line is handled in order, a final line
    /// with no newline is still a request, a line over `MAX_LINE` ends the connection. A newly
    /// authenticated hub connection (TCP) replaces this one. `Ok` on a clean close.
    async fn read_requests(
        &self,
        conn_id: u64,
        buf: Vec<u8>,
        mut rd: BoxRead,
        mut dead_rx: watch::Receiver<bool>,
        tcp_gate: Option<&TcpGate>,
    ) -> Result<(), String> {
        let inner = &self.0;
        let mut chunk = vec![0u8; 16 * 1024];
        let mut lines = LineScanner::with_buffered(buf);
        loop {
            // Complete lines already buffered.
            loop {
                match lines.next_line() {
                    Scan::Line(line) => self.handle_line(conn_id, &line).await,
                    Scan::More => break,
                    Scan::TooLong => return Err(ERR_TOO_LONG.to_string()),
                }
            }
            let n = tokio::select! {
                r = rd.read(&mut chunk) => r,
                _ = dead_rx.wait_for(|d| *d) => {
                    return Err(format!("read {}: use of closed network connection", inner.net));
                }
                _ = async {
                    match tcp_gate {
                        Some(g) => g.newcomer().await,
                        None => std::future::pending().await,
                    }
                } => {
                    crate::panic_hook::err_line(
                        "portico-transport: ipc: a newly authenticated hub connection replaced the current one",
                    );
                    return Ok(());
                }
            };
            match n {
                Ok(0) => {
                    // A final line with no newline is still a request.
                    if let Some(line) = lines.finish() {
                        self.handle_line(conn_id, &line).await;
                    }
                    return Ok(());
                }
                Ok(n) => lines.push(&chunk[..n]),
                Err(e) => return Err(format!("read {}: {}", inner.net, gojson::io_err(&e))),
            }
        }
    }

    /// Closes the current connection from the outside (the task that was serving it died): the
    /// write half is dropped, which closes the socket, and a blocked reader is told.
    pub async fn drop_connection(&self) {
        let mut st = self.0.state.lock().await;
        if let Some(c) = st.conn.as_mut() {
            c.writer = None;
            c.dead.send_replace(true);
        }
    }

    /// The next number for a connection or a lane (one counter, ids start at 1; the two kinds
    /// are never compared, R1-146).
    fn next_id(&self) -> u64 {
        self.0.next_id.fetch_add(1, Ordering::SeqCst) + 1
    }

    fn accept_failed(&self, e: std::io::Error) -> Result<(), String> {
        if self.is_closed() {
            return Ok(());
        }
        Err(format!("ipc: accept: {}", gojson::io_err(&e)))
    }

    async fn handle_line(&self, conn_id: u64, line: &[u8]) {
        let Some(req) = parse_request(line) else {
            return; // malformed line: drop it, don't kill the connection
        };
        let route = self.0.handlers.read().unwrap().get(&req.method).cloned();
        let Some(route) = route else {
            let r = Err(IpcError::new("unknown_method", req.method.clone()));
            self.write_response(conn_id, response_line(req.id, &r))
                .await;
            return;
        };
        match route.dispatch {
            Dispatch::Inline => self.run(conn_id, route.handler, req, true).await,
            Dispatch::Keyed(key_fn) => {
                let key = key_fn(raw_str(&req.params));
                if key.is_empty() {
                    self.spawn_run(conn_id, route.handler, req);
                } else {
                    self.run_keyed(conn_id, key, route.handler, req);
                }
            }
            Dispatch::Plain => self.spawn_run(conn_id, route.handler, req),
        }
    }

    fn spawn_run(&self, conn_id: u64, h: Arc<dyn Handler>, req: Request) {
        let me = self.clone();
        tokio::spawn(async move { me.run(conn_id, h, req, false).await });
    }

    /// Executes one request and writes its response to the connection it arrived on.
    ///
    /// A panic in the handler (any dispatch class) is answered with `internal_error` and the
    /// connection and the process carry on (R1-13; Go has no `recover()`: there the process ends
    /// and `hub.py` restarts it, PARITY 300).
    ///
    /// An INLINE handler (the `media_*` lane: 50 frames a second per call) runs under
    /// [`crate::guard::catch`], without a task of its own (R2-109). Every other handler runs on a
    /// task of its own, and that is not only for the panic: the hop gives a driver's own task
    /// the chance to report what the handler just did (the `answered` after `answer`) before the
    /// reply is written, which is the order Go's transcript has (the `T-gate_sip_server`
    /// conformance transcript fails without it: the reply comes first).
    async fn run(&self, conn_id: u64, h: Arc<dyn Handler>, req: Request, inline: bool) {
        let params = req.params;
        let r = if inline {
            match crate::guard::catch(h.call(params)).await {
                Ok(r) => r,
                Err(()) => Err(IpcError::new("internal_error", "handler panicked")),
            }
        } else {
            match tokio::spawn(async move { h.call(params).await }).await {
                Ok(r) => r,
                Err(e) if e.is_panic() => Err(IpcError::new("internal_error", "handler panicked")),
                Err(_) => Err(IpcError::new("internal_error", "handler cancelled")),
            }
        };
        let barrier = lock(&self.0.reply_barrier).clone();
        if let Some(b) = barrier {
            b().await;
        }
        self.write_response(conn_id, response_line(req.id, &r))
            .await;
    }

    /// Queues `req` behind the previous request for `key` and runs it on its own task once that
    /// one has finished. The queue position is taken here, on the read loop, so arrival order
    /// is preserved.
    fn run_keyed(&self, conn_id: u64, key: String, h: Arc<dyn Handler>, req: Request) {
        let (done_tx, done_rx) = oneshot::channel::<()>();
        let my_id = self.next_id();
        let prev = lock(&self.0.lanes).insert(key.clone(), (my_id, done_rx));
        let me = self.clone();
        tokio::spawn(async move {
            if let Some((_, prev_done)) = prev {
                let _ = prev_done.await; // resolves when the predecessor finishes (or is dropped)
            }
            // Release the lane even if the handler panics.
            struct Release {
                me: Server,
                key: String,
                my_id: u64,
                _done: oneshot::Sender<()>,
            }
            impl Drop for Release {
                fn drop(&mut self) {
                    let mut lanes = lock(&self.me.0.lanes);
                    if lanes.get(&self.key).map(|(id, _)| *id) == Some(self.my_id) {
                        lanes.remove(&self.key);
                    }
                }
            }
            let _release = Release {
                me: me.clone(),
                key,
                my_id,
                _done: done_tx,
            };
            me.run(conn_id, h, req, false).await;
        });
    }

    /// Writes `line` to the connection `conn_id` only if it is still the live one: a handler
    /// that outlives its connection must not write its reply to a later one (request ids
    /// restart per connection).
    async fn write_response(&self, conn_id: u64, line: String) {
        let mut st = self.0.state.lock().await;
        if st.conn.as_ref().map(|c| c.id) == Some(conn_id) {
            let _ = self.encode(&mut st, line.as_bytes()).await;
        }
    }

    /// Writes under a write deadline; the caller holds the state lock. A failed or timed-out
    /// write may have left half a line on the stream, so the connection is closed (`serve` then
    /// returns and Python reconnects).
    async fn encode(&self, st: &mut State, bytes: &[u8]) -> Result<(), String> {
        let wt = *lock(&self.0.write_timeout);
        let Some(conn) = st.conn.as_mut() else {
            return Err("ipc: no connection".into());
        };
        let Some(w) = conn.writer.as_mut() else {
            return Err(format!(
                "write {}: use of closed network connection",
                self.0.net
            ));
        };
        let r = tokio::time::timeout(wt, w.write_all(bytes)).await;
        let err = match r {
            Ok(Ok(())) => return Ok(()),
            Ok(Err(e)) => format!("write {}: {}", self.0.net, gojson::io_err(&e)),
            Err(_) => format!("write {}: i/o timeout", self.0.net),
        };
        conn.writer = None;
        conn.dead.send_replace(true);
        Err(err)
    }

    /// Sends an unsolicited notification to the connected Python side. Errors (including
    /// "nothing connected yet") are the caller's to decide what to do with.
    pub async fn notify(&self, method: &str, params: &Value) -> Result<(), String> {
        let mut st = self.0.state.lock().await;
        if st.conn.is_none() {
            return Err("ipc: no connection".into());
        }
        let line = notification_line(method, params);
        self.encode(&mut st, line.as_bytes()).await
    }

    /// [`Server::notify`] with `params` already serialised (a Go struct's field order, which a
    /// sorted `Value` cannot express). `params_json` must be valid JSON.
    pub async fn notify_raw(&self, method: &str, params_json: &str) -> Result<(), String> {
        let mut st = self.0.state.lock().await;
        if st.conn.is_none() {
            return Err("ipc: no connection".into());
        }
        let mut line = String::from("{\"method\":");
        gojson::push_string(&mut line, method);
        line.push_str(",\"params\":");
        line.push_str(params_json);
        line.push_str("}\n");
        self.encode(&mut st, line.as_bytes()).await
    }

    /// Stops accepting and closes the live connection, so a blocked `serve` returns `Ok`
    /// instead of reading on until the peer hangs up. Removes the socket file (Go's
    /// `listener.Close()` unlinks it).
    pub async fn close(&self) {
        let inner = &self.0;
        inner.closed.store(true, Ordering::SeqCst);
        inner.close_tx.send_replace(true);
        match &inner.listener {
            Listener::Unix { path, .. } => {
                if !inner.unlinked.swap(true, Ordering::SeqCst) {
                    let _ = std::fs::remove_file(path);
                }
            }
            // Go's `listener.Close()` frees the port at once: take our handle out; a `serve`
            // blocked in `accept` drops its own when the close signal wakes it.
            Listener::Tcp { l, gate, .. } => {
                lock(l).take();
                // the accepting task holds a handle too: end it, so the port is free on return
                gate.stop_acceptor().await;
            }
        }
        let mut st = inner.state.lock().await;
        if let Some(c) = st.conn.as_mut() {
            c.writer = None;
            c.dead.send_replace(true);
        }
    }
}

#[cfg(test)]
mod tests {
    //! Ports of `transport/internal/ipc/ipc_test.go` and `umask_test.go`.
    //! `umask_test.go` only defines `mustUmask`, the helper `TestListenSocketIsOwnerOnly`
    //! uses; it is `libc::umask` here.

    use super::*;
    use serde_json::json;
    use std::os::unix::fs::PermissionsExt;
    use tokio::io::{AsyncBufReadExt, BufReader};
    use tokio::net::UnixStream;

    #[allow(unsafe_code)]
    fn must_umask(m: u32) -> u32 {
        unsafe { libc::umask(m as libc::mode_t) as u32 }
    }

    fn tmp_sock() -> (tempdir::Dir, PathBuf) {
        let d = tempdir::Dir::new();
        let p = d.0.join("hub.sock");
        (d, p)
    }

    /// Tiny TempDir (no extra dependency).
    mod tempdir {
        use std::path::PathBuf;
        use std::sync::atomic::{AtomicU64, Ordering};
        pub struct Dir(pub PathBuf);
        static N: AtomicU64 = AtomicU64::new(0);
        impl Dir {
            pub fn new() -> Dir {
                let p = std::env::temp_dir().join(format!(
                    "ipc-test-{}-{}",
                    std::process::id(),
                    N.fetch_add(1, Ordering::SeqCst)
                ));
                std::fs::create_dir_all(&p).unwrap();
                Dir(p)
            }
        }
        impl Drop for Dir {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
    }

    // TestListenSocketIsOwnerOnly
    #[tokio::test]
    async fn listen_socket_is_owner_only() {
        let old = must_umask(0);
        let (_d, sock) = tmp_sock();
        let srv = Server::listen(&sock);
        must_umask(old);
        let _srv = srv.unwrap();
        let mode = std::fs::metadata(&sock).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "socket mode = {mode:o}, want 600");
    }

    async fn read_line(r: &mut BufReader<tokio::net::unix::OwnedReadHalf>) -> String {
        let mut s = String::new();
        let n = tokio::time::timeout(Duration::from_secs(3), r.read_line(&mut s))
            .await
            .expect("waiting for a response: timeout")
            .expect("read");
        assert!(n > 0, "connection closed while waiting for a response");
        s
    }

    async fn read_id(r: &mut BufReader<tokio::net::unix::OwnedReadHalf>) -> i64 {
        let line = read_line(r).await;
        let v: Value = serde_json::from_str(&line).unwrap();
        v["id"].as_i64().unwrap()
    }

    // Close must end a Serve that is blocked reading a live connection, and that is an
    // orderly stop (Ok), not a read error.
    // TestCloseEndsServeOnLiveConnection
    #[tokio::test]
    async fn close_ends_serve_on_live_connection() {
        let (_d, sock) = tmp_sock();
        let srv = Server::listen(&sock).unwrap();
        let s2 = srv.clone();
        let done = tokio::spawn(async move { s2.serve().await });
        let c = UnixStream::connect(&sock).await.unwrap();
        let (rd, mut wr) = c.into_split();
        let mut rd = BufReader::new(rd);
        // One round trip proves Serve has accepted and is in its read loop.
        wr.write_all(b"{\"id\":1,\"method\":\"nope\"}\n")
            .await
            .unwrap();
        read_line(&mut rd).await;
        srv.close().await;
        let r = tokio::time::timeout(Duration::from_secs(3), done)
            .await
            .expect("Serve still blocked after Close")
            .unwrap();
        assert_eq!(r, Ok(()), "Serve = {r:?}, want Ok");
    }

    async fn dial_server(
        srv: &Server,
        sock: &Path,
    ) -> (
        tokio::net::unix::OwnedWriteHalf,
        BufReader<tokio::net::unix::OwnedReadHalf>,
    ) {
        let s = srv.clone();
        tokio::spawn(async move {
            let _ = s.serve().await;
        });
        let c = UnixStream::connect(sock).await.unwrap();
        let (rd, wr) = c.into_split();
        (wr, BufReader::new(rd))
    }

    // A slow request must not hold up the read loop: an inline media_frame sent after it is
    // answered while it is still blocked.
    // TestSlowHandlerDoesNotBlockInlineFrames
    #[tokio::test]
    async fn slow_handler_does_not_block_inline_frames() {
        let (_d, sock) = tmp_sock();
        let srv = Server::listen(&sock).unwrap();
        let release = Arc::new(tokio::sync::Notify::new());
        let rel = release.clone();
        srv.handle("slow", move |_p: Params| {
            let rel = rel.clone();
            async move {
                rel.notified().await;
                Ok(json!(true).into())
            }
        });
        srv.handle_inline("media_frame", |_p: Params| async { Ok(json!(true).into()) });
        let (mut c, mut r) = dial_server(&srv, &sock).await;

        c.write_all(b"{\"id\":1,\"method\":\"slow\"}\n")
            .await
            .unwrap();
        for i in 2..12 {
            c.write_all(format!("{{\"id\":{i},\"method\":\"media_frame\"}}\n").as_bytes())
                .await
                .unwrap();
        }
        for i in 2..12 {
            let got = read_id(&mut r).await;
            assert_eq!(got, i, "response id = {got}, want {i}"); // inline: in arrival order
        }
        release.notify_one();
        let got = read_id(&mut r).await;
        assert_eq!(got, 1, "slow response id = {got}, want 1");
        srv.close().await;
    }

    // Keyed requests run one at a time in arrival order per key; another key (and an unkeyed
    // request) is not held up by them.
    // TestKeyedRequestsAreOrderedPerKey
    #[tokio::test]
    async fn keyed_requests_are_ordered_per_key() {
        let (_d, sock) = tmp_sock();
        let srv = Server::listen(&sock).unwrap();
        let key_of: KeyFn = Arc::new(|raw: &str| {
            serde_json::from_str::<Value>(raw)
                .ok()
                .and_then(|v| v.get("K").and_then(|k| k.as_str().map(String::from)))
                .unwrap_or_default()
        });
        let release = Arc::new(tokio::sync::Notify::new());
        let order = Arc::new(StdMutex::new(Vec::<i64>::new()));
        let (rel, ord) = (release.clone(), order.clone());
        srv.handle_keyed("k", key_of, move |p: Params| {
            let (rel, ord) = (rel.clone(), ord.clone());
            async move {
                let v: Value = serde_json::from_str(raw_str(&p)).unwrap();
                let n = v["N"].as_i64().unwrap();
                if n == 1 {
                    rel.notified().await; // the first "a" request blocks; later ones must queue behind it
                }
                ord.lock().unwrap().push(n);
                Ok(json!(true).into())
            }
        });
        srv.handle("free", |_p: Params| async { Ok(json!(true).into()) });
        let (mut c, mut r) = dial_server(&srv, &sock).await;

        for line in [
            r#"{"id":1,"method":"k","params":{"K":"a","N":1}}"#,
            r#"{"id":2,"method":"k","params":{"K":"a","N":2}}"#,
            r#"{"id":3,"method":"k","params":{"K":"b","N":3}}"#,
            r#"{"id":4,"method":"free"}"#,
        ] {
            c.write_all(format!("{line}\n").as_bytes()).await.unwrap();
        }
        let mut got = std::collections::HashSet::new();
        got.insert(read_id(&mut r).await);
        got.insert(read_id(&mut r).await);
        assert!(
            got.contains(&3) && got.contains(&4),
            "other key / unkeyed request were held up: first two responses {got:?}"
        );
        release.notify_one();
        let mut got = std::collections::HashSet::new();
        got.insert(read_id(&mut r).await);
        got.insert(read_id(&mut r).await);
        assert!(
            got.contains(&1) && got.contains(&2),
            "keyed responses = {got:?}, want 1 and 2"
        );
        let order = order.lock().unwrap().clone();
        // Ported literally, including Go's `order[1] != 1 && order[0] != 3` precedence.
        assert!(
            !(order.len() != 3 || (order[1] != 1 && order[0] != 3)),
            "execution order = {order:?}"
        );
        let a: Vec<i64> = order.iter().copied().filter(|n| *n != 3).collect();
        assert_eq!(a, vec![1, 2], "key a ran out of order: {a:?}");
        srv.close().await;
    }

    // A reply from a handler that outlives its connection is dropped, not written to the next
    // connection (ids restart per connection).
    // TestStaleReplyNotSentToNextConnection
    #[tokio::test]
    async fn stale_reply_not_sent_to_next_connection() {
        let (_d, sock) = tmp_sock();
        let srv = Server::listen(&sock).unwrap();
        let release = Arc::new(tokio::sync::Notify::new());
        let finished = Arc::new(tokio::sync::Notify::new());
        let (rel, fin) = (release.clone(), finished.clone());
        srv.handle("slow", move |_p: Params| {
            let (rel, fin) = (rel.clone(), fin.clone());
            async move {
                rel.notified().await;
                fin.notify_one();
                Ok(json!(true).into())
            }
        });
        srv.handle("fast", |_p: Params| async { Ok(json!(true).into()) });
        let s = srv.clone();
        tokio::spawn(async move {
            let _ = s.serve().await;
        });
        let mut c1 = UnixStream::connect(&sock).await.unwrap();
        c1.write_all(b"{\"id\":7,\"method\":\"slow\"}\n")
            .await
            .unwrap();
        tokio::time::sleep(Duration::from_millis(100)).await;
        drop(c1);
        tokio::time::sleep(Duration::from_millis(100)).await; // Serve returns; start the next accept
        let s = srv.clone();
        tokio::spawn(async move {
            let _ = s.serve().await;
        });
        let c2 = UnixStream::connect(&sock).await.unwrap();
        let (rd, mut wr) = c2.into_split();
        let mut r2 = BufReader::new(rd);
        wr.write_all(b"{\"id\":1,\"method\":\"fast\"}\n")
            .await
            .unwrap();
        let got = read_id(&mut r2).await;
        assert_eq!(
            got, 1,
            "first reply on the new connection has id {got}, want 1"
        );
        release.notify_one();
        finished.notified().await;
        tokio::time::sleep(Duration::from_millis(100)).await;
        let mut line = String::new();
        let leaked =
            tokio::time::timeout(Duration::from_millis(200), r2.read_line(&mut line)).await;
        if let Ok(Ok(n)) = leaked {
            assert!(n == 0, "stale reply leaked to the new connection: {line:?}");
        }
        srv.close().await;
    }

    // A pre-serialised result and notification are written verbatim (struct field order), a
    // Value result still sorts its keys.
    #[test]
    fn raw_reply_is_written_verbatim() {
        let raw = response_line(7, &Ok(Reply::Raw("{\"b\":1,\"a\":2}".into())));
        assert_eq!(raw, "{\"id\":7,\"result\":{\"b\":1,\"a\":2}}\n");
        let sorted = response_line(8, &Ok(json!({"b": 1, "a": 2}).into()));
        assert_eq!(sorted, "{\"id\":8,\"result\":{\"a\":2,\"b\":1}}\n");
    }

    // A peer that never reads must not block Notify forever (it holds the write lock, and the
    // SIP read loop calls it): the write times out and the connection is closed so the peer
    // reconnects.
    // TestNotifyToStalledPeerTimesOut
    #[tokio::test]
    async fn notify_to_stalled_peer_times_out() {
        let (_d, sock) = tmp_sock();
        let srv = Server::listen(&sock).unwrap();
        srv.set_write_timeout(Duration::from_millis(200));
        let s = srv.clone();
        let done = tokio::spawn(async move { s.serve().await });
        let _c = UnixStream::connect(&sock).await.unwrap(); // never read from it
        for _ in 0..200 {
            if srv.has_connection().await {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        let payload = Value::String("x".repeat(64 << 10));
        let s = srv.clone();
        let finished = tokio::spawn(async move {
            let mut err = Ok(());
            let mut i = 0;
            while i < 10000 && err.is_ok() {
                // fill the socket buffer
                err = s.notify("ringing", &payload).await;
                i += 1;
            }
            err
        });
        let r = tokio::time::timeout(Duration::from_secs(5), finished)
            .await
            .expect("Notify blocked on a stalled peer")
            .unwrap();
        assert!(r.is_err(), "Notify never failed against a stalled peer");
        tokio::time::timeout(Duration::from_secs(3), done)
            .await
            .expect("Serve still running after a failed write")
            .unwrap()
            .ok();
        srv.close().await;
    }
    // R1-13: a panic in a handler of any dispatch class is answered with `internal_error`; the
    // connection, the process and the keyed lane carry on (mutation: run the handler on this task).
    #[tokio::test]
    async fn a_panicking_handler_is_answered_in_every_dispatch_class() {
        let (_d, sock) = tmp_sock();
        let srv = Server::listen(&sock).unwrap();
        let boom = |_p: Params| async {
            if true {
                panic!("handler exploded");
            }
            Ok::<Reply, IpcError>(Reply::Json(json!({})))
        };
        srv.handle("plain_boom", boom);
        srv.handle_inline("inline_boom", boom);
        srv.handle_keyed("keyed_boom", Arc::new(|_| "k".to_string()), boom);
        srv.handle("ok", |_p: Params| async {
            Ok::<Reply, IpcError>(Reply::Json(json!({"fine": true})))
        });
        let (mut wr, mut rd) = dial_server(&srv, &sock).await;
        for (i, m) in [
            "plain_boom",
            "inline_boom",
            "keyed_boom",
            "keyed_boom",
            "ok",
        ]
        .iter()
        .enumerate()
        {
            let id = i as i64 + 1;
            wr.write_all(format!("{{\"id\":{id},\"method\":\"{m}\"}}\n").as_bytes())
                .await
                .unwrap();
            let line = read_line(&mut rd).await;
            if *m == "ok" {
                assert_eq!(
                    line,
                    format!("{{\"id\":{id},\"result\":{{\"fine\":true}}}}\n")
                );
            } else {
                assert_eq!(
                    line,
                    format!("{{\"id\":{id},\"error\":{{\"code\":\"internal_error\",\"message\":\"handler panicked\"}}}}\n"),
                    "{m}"
                );
            }
        }
        srv.close().await;
    }

    // R1-2: the barrier runs after the handler and before its reply is written.
    #[tokio::test]
    async fn the_reply_barrier_runs_before_the_reply() {
        let (_d, sock) = tmp_sock();
        let srv = Server::listen(&sock).unwrap();
        let s2 = srv.clone();
        srv.set_reply_barrier(Arc::new(move || {
            let s = s2.clone();
            Box::pin(async move {
                let _ = s.notify("event", &json!({"n": 1})).await;
            })
        }));
        srv.handle("ok", |_p: Params| async {
            Ok::<Reply, IpcError>(Reply::Json(json!({})))
        });
        let (mut wr, mut rd) = dial_server(&srv, &sock).await;
        wr.write_all(b"{\"id\":1,\"method\":\"ok\"}\n")
            .await
            .unwrap();
        assert!(read_line(&mut rd)
            .await
            .starts_with("{\"method\":\"event\""));
        assert!(read_line(&mut rd).await.starts_with("{\"id\":1,"));
        srv.close().await;
    }
}
