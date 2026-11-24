//! Port of `endpoint.go`: the UDP socket shared by everything this package does with one peer,
//! request/response correlation by branch with the RFC 3261 retransmission timers, and the
//! per-Call-ID dispatch of inbound requests. Task layout and cancellation: DESIGN.md.

use std::collections::{HashMap, VecDeque};
use std::future::Future;
use std::ops::Deref;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::net::UdpSocket;
use tokio::sync::mpsc;
use tokio::time::{sleep, Instant};

use super::message::{find_sub, parse_request, parse_response, ParsedRequest, Response};
use super::Error;
use crate::cancel::CancelToken;
use crate::gojson::io_err;
use crate::gostd::net::{local_host, resolve_udp};
use crate::gostd::strconv::go_atoi_lenient;
use crate::util::{lock, rand_hex};

struct WriteErr {
    text: String,
    /// ICMP port unreachable reported by the connected socket (a station that is down).
    refused: bool,
    /// The endpoint is closed.
    closed: bool,
}

type BoxFut = Pin<Box<dyn Future<Output = ()> + Send>>;
/// A queued handler call; the handler is invoked (and its future built) inside the drain task.
/// Caps on the per-Call-ID dispatch map (DEV-796 S2).
const DISPATCH_MAX_KEYS: usize = 256;
const DISPATCH_MAX_QUEUE: usize = 32;

type Job = Box<dyn FnOnce() -> BoxFut + Send>;
type Handler = Arc<dyn Fn(IncomingRequest) -> BoxFut + Send + Sync>;

/// Transaction timers (RFC 3261 17.1). Go keeps them in package variables so tests can shrink
/// them; here they belong to the endpoint ([`Endpoint::set_timers`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Timers {
    /// Retransmit interval, doubled each time.
    pub t1: Duration,
    /// Retransmit interval cap (non-INVITE).
    pub t2: Duration,
    /// Bound on the wait for the FIRST response of any request.
    pub send_timeout: Duration,
    /// An INVITE's wait for a final response once a 1xx has arrived; restarted by every 1xx.
    pub invite_ring_timeout: Duration,
    /// How long to keep listening after CANCEL for the 487 (or a 200 that crossed it).
    pub cancel_wait: Duration,
}

impl Default for Timers {
    fn default() -> Self {
        Timers {
            t1: Duration::from_millis(500),
            t2: Duration::from_secs(4),
            send_timeout: Duration::from_secs(5),
            invite_ring_timeout: Duration::from_secs(3 * 60),
            cancel_wait: Duration::from_secs(2),
        }
    }
}

#[derive(Default)]
struct State {
    pending: HashMap<String, mpsc::Sender<Response>>,
    handlers: HashMap<String, Handler>,
}

struct Inner {
    /// `None` once closed: the read loop owns the last handle while it ends, then the fd goes.
    socket: Mutex<Option<Arc<UdpSocket>>>,
    host: String,
    local_addr: String,
    state: Mutex<State>,
    /// Per-Call-ID queues. A key present means a drain task is running (or about to exit).
    dq: Mutex<HashMap<String, VecDeque<Job>>>,
    timers: Mutex<Timers>,
    closed: AtomicBool,
    stop: CancelToken,
    /// The supervisor of the read loop (it logs the loop's panic); kept here, not detached.
    reader: Mutex<Option<tokio::task::JoinHandle<()>>>,
}

/// Owns the single UDP socket used for everything with one remote peer (one device). Cheap to
/// clone; all clones share the socket. Call [`Endpoint::close`] when done (as in Go, dropping
/// does not stop the read loop).
#[derive(Clone)]
pub struct Endpoint {
    inner: Arc<Inner>,
}

impl Endpoint {
    /// `NewEndpoint`: resolves `host:port` (port 0 means 5060), connects a UDP socket to it and
    /// starts the read loop. Must be called inside a tokio runtime.
    pub async fn new(host: &str, port: i64) -> Result<Endpoint, Error> {
        let port = if port == 0 { 5060 } else { port };
        let addr = resolve_udp(host, port)
            .await
            .map_err(|e| Error::msg(format!("sip: resolving {host}:{port}: {e}")))?;
        let bind: std::net::SocketAddr = if addr.is_ipv4() {
            ([0, 0, 0, 0], 0).into()
        } else {
            ([0u16; 8], 0).into()
        };
        let dial_err =
            |e: std::io::Error| Error::msg(format!("sip: dialing {host}:{port}: {}", io_err(&e)));
        let socket = UdpSocket::bind(bind).await.map_err(dial_err)?;
        socket.connect(addr).await.map_err(dial_err)?;
        let local_addr = socket.local_addr().map_err(dial_err)?.to_string();
        let socket = Arc::new(socket);
        let inner = Arc::new(Inner {
            socket: Mutex::new(Some(socket.clone())),
            host: host.to_string(),
            local_addr,
            state: Mutex::default(),
            dq: Mutex::default(),
            timers: Mutex::new(Timers::default()),
            closed: AtomicBool::new(false),
            stop: CancelToken::new(),
            reader: Mutex::new(None),
        });
        let read = tokio::spawn(read_loop(inner.clone(), socket));
        let watched = inner.clone();
        let supervisor = tokio::spawn(async move {
            if let Err(e) = read.await {
                if e.is_panic() {
                    // Without the loop nothing is read any more: fail every later write and wait
                    // loudly instead of leaving the endpoint deaf.
                    crate::panic_hook::err_line(
                        "portico-transport: sip: the endpoint read loop panicked; the endpoint is closed",
                    );
                    watched.closed.store(true, Ordering::SeqCst);
                    watched.stop.cancel();
                }
            }
        });
        *lock(&inner.reader) = Some(supervisor);
        Ok(Endpoint { inner })
    }

    pub fn local_addr(&self) -> &str {
        &self.inner.local_addr
    }

    pub fn host(&self) -> &str {
        &self.inner.host
    }

    /// The IP of the interface this endpoint's socket sends from (what goes into Via/Contact).
    pub fn local_host(&self) -> String {
        local_host(&self.inner.local_addr)
    }

    /// Closes the socket: the read loop ends, writes fail from now on, and the file descriptor
    /// and the bound port are released as soon as the read loop has seen the cancel (the next time
    /// it is polled; nothing else keeps the socket). The handler table and the pending-response
    /// table are emptied too: a handler closure that holds an `IncomingRequest` (and so a strong
    /// `Endpoint`) would otherwise keep this endpoint alive for ever (R1-17). Idempotent.
    pub fn close(&self) -> Result<(), Error> {
        self.inner.closed.store(true, Ordering::SeqCst);
        self.inner.stop.cancel();
        lock(&self.inner.socket).take();
        {
            let mut st = lock(&self.inner.state);
            st.handlers.clear();
            st.pending.clear(); // the waiters see their channel end
        }
        lock(&self.inner.reader).take(); // the supervisor ends with the read loop
        Ok(())
    }

    pub fn set_timers(&self, t: Timers) {
        *lock(&self.inner.timers) = t;
    }

    fn timers(&self) -> Timers {
        *lock(&self.inner.timers)
    }

    /// Registers the handler for an inbound request method (INVITE, BYE...). Call before any
    /// inbound traffic is expected: there is no queueing for a method with no handler yet.
    /// Handlers are async and must not block the thread (DESIGN.md).
    pub fn handle<F, Fut>(&self, method: &str, h: F)
    where
        F: Fn(IncomingRequest) -> Fut + Send + Sync + 'static,
        Fut: Future<Output = ()> + Send + 'static,
    {
        if self.inner.closed.load(Ordering::SeqCst) {
            return;
        }
        let h: Handler = Arc::new(move |r| Box::pin(h(r)));
        lock(&self.inner.state)
            .handlers
            .insert(method.to_string(), h);
    }

    /// Sends a raw, response-less message (an ACK, or an in-dialog response).
    pub async fn write(&self, raw: &[u8]) -> Result<(), Error> {
        self.write_raw(raw).await.map_err(|e| {
            if e.closed {
                Error::Closed
            } else {
                Error::msg(e.text)
            }
        })
    }

    async fn write_raw(&self, raw: &[u8]) -> Result<(), WriteErr> {
        let closed = || WriteErr {
            text: Error::Closed.to_string(),
            refused: false,
            closed: true,
        };
        if self.inner.closed.load(Ordering::SeqCst) {
            return Err(closed());
        }
        let Some(socket) = lock(&self.inner.socket).clone() else {
            return Err(closed());
        };
        socket.send(raw).await.map(|_| ()).map_err(|e| WriteErr {
            text: io_err(&e),
            refused: e.kind() == std::io::ErrorKind::ConnectionRefused,
            closed: false,
        })
    }

    fn unregister(&self, branch: &str) {
        lock(&self.inner.state).pending.remove(branch);
    }

    /// `SendRequest`: sends a pre-built request and waits for a matching final (>= 200) response,
    /// correlated by branch. Provisional responses go to `on_provisional` without ending the
    /// wait. The request is retransmitted on the RFC 3261 schedule until any response arrives
    /// and abandoned after `send_timeout` without one. For an INVITE a 1xx extends the wait to
    /// `invite_ring_timeout` (Timer C); giving up after a 1xx sends CANCEL and ACKs the 487;
    /// every non-2xx final to an INVITE is ACKed here, the ACK of a 2xx belongs to the dialog
    /// layer. Dropping the future abandons the request; an INVITE that was already ringing is
    /// CANCELled in a detached task (which also ACKs a 487 and ACK+BYEs a crossed 200).
    pub async fn send_request(
        &self,
        raw: &[u8],
        branch: &str,
        mut on_provisional: Option<&mut (dyn FnMut(&Response) + Send)>,
    ) -> Result<Response, Error> {
        let t = self.timers();
        let (tx, rx) = mpsc::channel::<Response>(4);
        lock(&self.inner.state)
            .pending
            .insert(branch.to_string(), tx);
        let invite = if raw.starts_with(b"INVITE ") {
            parse_request(raw).ok().map(Arc::new)
        } else {
            None
        };
        let mut guard = ReqGuard {
            ep: self.clone(),
            branch: branch.to_string(),
            rx: Some(rx),
            invite: invite.clone(),
            ringing: false,
            cancel_wait: t.cancel_wait,
        };

        // A refusal reported for an earlier datagram (the station was down) is consumed by this
        // send; try once more, and if the peer is still refusing let the retransmits carry on.
        for attempt in 0..2 {
            match self.write_raw(raw).await {
                Ok(()) => break,
                Err(e) if e.refused => {
                    if attempt == 1 {
                        break;
                    }
                }
                Err(e) => return Err(Error::msg(format!("sip: writing request: {}", e.text))),
            }
        }

        let mut interval = t.t1;
        let retransmit = sleep(interval);
        tokio::pin!(retransmit);
        let mut retransmitting = true;
        let deadline = sleep(t.send_timeout);
        tokio::pin!(deadline);
        let mut rx_open = true;
        loop {
            tokio::select! {
                // Not in Go (PARITY R1-B-3): a closed endpoint ends the wait now; Go's request
                // sits out its deadline (up to 5 s, or Timer C for a ringing INVITE).
                _ = self.inner.stop.cancelled() => return Err(Error::Closed),
                resp = guard.rx.as_mut().unwrap().recv(), if rx_open => {
                    let Some(resp) = resp else {
                        // our channel was replaced by a request reusing the branch: Go would
                        // block on it until the deadline
                        rx_open = false;
                        continue;
                    };
                    if resp.status_code < 200 {
                        retransmitting = false; // 17.1.1.2: a 1xx ends Timer A
                        if invite.is_some() {
                            guard.ringing = true;
                            deadline.as_mut().reset(Instant::now() + t.invite_ring_timeout);
                        }
                        if let Some(cb) = on_provisional.as_mut() {
                            cb(&resp);
                        }
                        continue;
                    }
                    guard.ringing = false;
                    if let Some(inv) = &invite {
                        if resp.status_code >= 300 {
                            let _ = self.write_raw(&ack_for_invite(inv, &resp)).await;
                        }
                    }
                    return Ok(resp);
                }
                _ = &mut retransmit, if retransmitting => {
                    // A refused retransmit (station rebooting) must not end the request.
                    if let Err(e) = self.write_raw(raw).await {
                        if !e.refused {
                            return Err(Error::msg(format!("sip: retransmitting request: {}", e.text)));
                        }
                    }
                    interval *= 2;
                    if invite.is_none() && interval > t.t2 {
                        interval = t.t2;
                    }
                    retransmit.as_mut().reset(Instant::now() + interval);
                }
                _ = &mut deadline => {
                    if let Some(inv) = &invite {
                        if guard.ringing {
                            // stays armed until cancel_invite returns: dropped mid-wait,
                            // the guard repeats the (harmless) CANCEL and keeps listening
                            self.cancel_invite(inv, guard.rx.as_mut().unwrap(), t.cancel_wait).await;
                            guard.ringing = false;
                        }
                    }
                    return Err(Error::Timeout);
                }
            }
        }
    }

    /// Abandons an INVITE we gave up on (RFC 3261 9.1): CANCEL shares the INVITE's Request-URI,
    /// Via branch, Call-ID, From, To and CSeq number. Then listens briefly on the INVITE's
    /// response channel so a 487 is ACKed (with its own To-tag) and a 200 to the INVITE that
    /// crossed the CANCEL is ACKed and torn down with a BYE. The CANCEL's own `200 OK` shares the
    /// channel (same branch) and is ignored: Go (endpoint.go:199-211) mistakes it for the crossed
    /// 200, sends an ACK and a BYE and never ACKs the 487.
    async fn cancel_invite(
        &self,
        invite: &ParsedRequest,
        rx: &mut mpsc::Receiver<Response>,
        cancel_wait: Duration,
    ) {
        let _ = self
            .write_raw(&build_in_dialog_like(
                "CANCEL",
                invite,
                invite.header_raw("To"),
                invite.header_raw("Via"),
                cseq_number(invite),
            ))
            .await;
        let wait = sleep(cancel_wait);
        tokio::pin!(wait);
        loop {
            tokio::select! {
                _ = self.inner.stop.cancelled() => return,
                resp = rx.recv() => {
                    let Some(resp) = resp else {
                        // Go would block on a channel nobody writes to until cancel_wait; a
                        // closed endpoint ends the wait now (PARITY 313, R2-84): `close` ends
                        // this channel AND cancels the token, so both arms can be ready
                        tokio::select! {
                            _ = &mut wait => {}
                            _ = self.inner.stop.cancelled() => {}
                        }
                        return;
                    };
                    if resp.status_code < 200 {
                        continue;
                    }
                    if cseq_method(&resp).eq_ignore_ascii_case("CANCEL") {
                        continue; // the CANCEL's own final response
                    }
                    if resp.status_code >= 300 {
                        let _ = self.write_raw(&ack_for_invite(invite, &resp)).await;
                        return;
                    }
                    // 200 to the INVITE crossed our CANCEL: ACK, then hang up.
                    let to = resp.header_raw("To").to_vec();
                    let _ = self.write_raw(&build_in_dialog_like(
                        "ACK", invite, &to, &new_branch_via(invite), cseq_number(invite))).await;
                    let _ = self.write_raw(&build_in_dialog_like(
                        "BYE", invite, &to, &new_branch_via(invite), cseq_number(invite) + 1)).await;
                    return;
                }
                _ = &mut wait => return,
            }
        }
    }

    /// Runs the job strictly after every earlier job queued under the same key (the Call-ID), so
    /// an INVITE, its CANCEL and the BYE are handled in arrival order; different dialogs run
    /// concurrently. The drain task exits when the key's queue is empty. The map holds at most
    /// `DISPATCH_MAX_KEYS` keys of `DISPATCH_MAX_QUEUE` jobs; the excess is dropped (DEV-796 S2).
    /// A retransmitted request is queued again (the handler must tolerate that).
    fn dispatch(&self, key: String, method: String, job: Job) {
        {
            let mut dq = lock(&self.inner.dq);
            if let Some(q) = dq.get_mut(&key) {
                // DEV-796 S2: a full per-key queue drops the excess request.
                if q.len() < DISPATCH_MAX_QUEUE {
                    q.push_back(job);
                }
                return;
            }
            if dq.len() >= DISPATCH_MAX_KEYS {
                return; // too many dialogs in flight: drop the new one (DEV-796 S2)
            }
            dq.insert(key.clone(), VecDeque::from([job]));
        }
        let inner = self.inner.clone();
        tokio::spawn(async move {
            loop {
                let next = {
                    let mut dq = lock(&inner.dq);
                    match dq.get_mut(&key).and_then(VecDeque::pop_front) {
                        Some(f) => f,
                        None => {
                            dq.remove(&key);
                            return;
                        }
                    }
                };
                // Own task, and the handler is called inside it, so neither a panicking
                // handler future nor a panic while building it can wedge the key or the read
                // loop (Go would crash the process).
                if let Err(e) = tokio::spawn(async move { next().await }).await {
                    // The panic payload is reported (scrubbed) by the process panic hook; this
                    // line names the request. Call-ID quoted: it is the peer's bytes.
                    if e.is_panic() {
                        crate::panic_hook::err_line(&format!(
                            "portico-transport: sip: the handler for {method} (Call-ID {}) panicked",
                            crate::gostd::strconv::go_quote(&crate::gostd::text::go_lossy(key.as_bytes()))
                        ));
                    }
                }
            }
        });
    }
}

/// Owns a request's pending slot. On drop it unregisters the branch; for an INVITE that was
/// ringing it first hands the response channel to a detached task that sends CANCEL and
/// completes the teardown.
struct ReqGuard {
    ep: Endpoint,
    branch: String,
    rx: Option<mpsc::Receiver<Response>>,
    invite: Option<Arc<ParsedRequest>>,
    ringing: bool,
    cancel_wait: Duration,
}

impl Drop for ReqGuard {
    fn drop(&mut self) {
        if self.ringing {
            if let (Some(mut rx), Some(inv), Ok(h)) = (
                self.rx.take(),
                self.invite.take(),
                tokio::runtime::Handle::try_current(),
            ) {
                let (ep, branch, cw) = (self.ep.clone(), self.branch.clone(), self.cancel_wait);
                h.spawn(async move {
                    ep.cancel_invite(&inv, &mut rx, cw).await;
                    ep.unregister(&branch);
                });
                return;
            }
        }
        self.ep.unregister(&self.branch);
    }
}

/// The only long-lived holder of the socket besides `Inner`: when it returns, the descriptor is
/// closed (`Endpoint::close` has already taken `Inner`'s handle).
async fn read_loop(inner: Arc<Inner>, socket: Arc<UdpSocket>) {
    let ep = Endpoint {
        inner: inner.clone(),
    };
    let mut buf = vec![0u8; 4096];
    loop {
        let r = tokio::select! {
            _ = inner.stop.cancelled() => return,
            r = socket.recv(&mut buf) => r,
        };
        let n = match r {
            Ok(n) => n,
            Err(e) => {
                // Go returns on any read error; on a connected UDP socket an ICMP "port
                // unreachable" (station down or rebooting) would then deafen the endpoint for
                // good. Only a close ends the loop.
                if inner.closed.load(Ordering::SeqCst) {
                    return;
                }
                if e.kind() != std::io::ErrorKind::ConnectionRefused {
                    tokio::select! {
                        _ = inner.stop.cancelled() => return,
                        _ = sleep(Duration::from_millis(50)) => {}
                    }
                }
                continue;
            }
        };
        let data = buf[..n].to_vec();
        if data.starts_with(b"SIP/2.0 ") {
            let Ok(resp) = parse_response(&data) else {
                continue;
            };
            let branch = branch_from_via(resp.header("Via"));
            let tx = lock(&inner.state).pending.get(&branch).cloned();
            if let Some(tx) = tx {
                let _ = tx.try_send(resp); // full: dropped
            }
            continue;
        }
        let Ok(req) = parse_request(&data) else {
            continue;
        };
        let h = lock(&inner.state).handlers.get(&req.method).cloned();
        if let Some(h) = h {
            // Off the read loop: a handler that blocks must not stop responses to our own
            // REGISTER/INVITE from being read.
            let key = req.header("Call-ID").to_string();
            let method = req.method.clone();
            let ir = IncomingRequest {
                parsed: req,
                endpoint: Some(ep.clone()),
                to_tag: String::new(),
            };
            ep.dispatch(key, method, Box::new(move || h(ir)));
        }
    }
}

/// The branch parameter of a Via header value (`""` if none).
pub fn branch_from_via(via: &str) -> String {
    let Some(idx) = via.find("branch=") else {
        return String::new();
    };
    let mut rest = &via[idx + "branch=".len()..];
    if let Some(semi) = rest.find(';') {
        rest = &rest[..semi];
    }
    rest.trim().to_string()
}

fn ack_for_invite(invite: &ParsedRequest, resp: &Response) -> Vec<u8> {
    build_in_dialog_like(
        "ACK",
        invite,
        resp.header_raw("To"),
        invite.header_raw("Via"),
        cseq_number(invite),
    )
}

fn cseq_number(r: &ParsedRequest) -> i64 {
    // `n, _ := strconv.Atoi(..)`: 0 on a syntax error, the clamped extreme on a range error.
    r.header("CSeq")
        .split_whitespace()
        .next()
        .map(go_atoi_lenient)
        .unwrap_or(0)
}

/// The method of a response's `CSeq` header (`""` if absent).
fn cseq_method(r: &Response) -> &str {
    r.header("CSeq").split_whitespace().nth(1).unwrap_or("")
}

/// The INVITE's Via with a fresh branch, for a request that starts its own transaction.
fn new_branch_via(invite: &ParsedRequest) -> Vec<u8> {
    let via = invite.header_raw("Via");
    match find_sub(via, b"branch=") {
        Some(i) => {
            let mut v = via[..i].to_vec();
            v.extend_from_slice(format!("branch=z9hG4bK{}", rand_hex(8)).as_bytes());
            v
        }
        None => via.to_vec(),
    }
}

fn push_header(b: &mut Vec<u8>, name: &str, value: &[u8]) {
    b.extend_from_slice(name.as_bytes());
    b.extend_from_slice(b": ");
    b.extend_from_slice(value);
    b.extend_from_slice(b"\r\n");
}

/// A request in the INVITE's dialog; the echoed Request-URI, Via, From, To and Call-ID are the
/// bytes received, not a UTF-8 repair of them.
fn build_in_dialog_like(
    method: &str,
    invite: &ParsedRequest,
    to: &[u8],
    via: &[u8],
    cseq: i64,
) -> Vec<u8> {
    let mut b = Vec::new();
    b.extend_from_slice(method.as_bytes());
    b.push(b' ');
    b.extend_from_slice(&invite.raw_uri);
    b.extend_from_slice(b" SIP/2.0\r\n");
    push_header(&mut b, "Via", via);
    push_header(&mut b, "Max-Forwards", b"70");
    push_header(&mut b, "From", invite.header_raw("From"));
    push_header(&mut b, "To", to);
    push_header(&mut b, "Call-ID", invite.header_raw("Call-ID"));
    push_header(&mut b, "CSeq", format!("{cseq} {method}").as_bytes());
    b.extend_from_slice(b"Content-Length: 0\r\n\r\n");
    b
}

/// A [`ParsedRequest`] plus the means to respond to it.
#[derive(Clone)]
pub struct IncomingRequest {
    pub parsed: ParsedRequest,
    endpoint: Option<Endpoint>,
    /// If set by the handler at ring time, the To-tag `answer_incoming` reuses in its 200 OK so
    /// the 180 and the 200 carry one dialog tag.
    pub to_tag: String,
}

impl Deref for IncomingRequest {
    type Target = ParsedRequest;
    fn deref(&self) -> &ParsedRequest {
        &self.parsed
    }
}

impl IncomingRequest {
    /// A request that did not arrive on an endpoint (`local_host()` is `""`; `respond` fails).
    pub fn detached(parsed: ParsedRequest) -> Self {
        IncomingRequest {
            parsed,
            endpoint: None,
            to_tag: String::new(),
        }
    }

    pub(crate) fn endpoint(&self) -> Option<&Endpoint> {
        self.endpoint.as_ref()
    }

    /// The local address of the endpoint this request arrived on (`""` for none).
    pub fn local_host(&self) -> String {
        self.endpoint
            .as_ref()
            .map(Endpoint::local_host)
            .unwrap_or_default()
    }

    /// Sends a response, copying Via/From/Call-ID/CSeq verbatim (RFC 3261). `to_tag` is added to
    /// the To header unless it already has a tag.
    pub async fn respond(
        &self,
        status: i64,
        reason: &str,
        to_tag: &str,
        body: &str,
    ) -> Result<(), Error> {
        let mut b: Vec<u8> = format!("SIP/2.0 {status} {reason}\r\n").into_bytes();
        for v in self.headers_raw("via") {
            push_header(&mut b, "Via", v);
        }
        push_header(&mut b, "From", self.header_raw("From"));
        let mut to = self.header_raw("To").to_vec();
        if !to_tag.is_empty() && find_sub(&to, b"tag=").is_none() {
            to.extend_from_slice(format!(";tag={to_tag}").as_bytes());
        }
        push_header(&mut b, "To", &to);
        push_header(&mut b, "Call-ID", self.header_raw("Call-ID"));
        push_header(&mut b, "CSeq", self.header_raw("CSeq"));
        if !body.is_empty() {
            b.extend_from_slice(b"Content-Type: application/sdp\r\n");
            b.extend_from_slice(
                format!("Content-Length: {}\r\n\r\n{}", body.len(), body).as_bytes(),
            );
        } else {
            b.extend_from_slice(b"Content-Length: 0\r\n\r\n");
        }
        match &self.endpoint {
            Some(e) => e.write(&b).await,
            None => Err(Error::msg("sip: request has no endpoint")),
        }
    }
}

#[cfg(test)]
pub(crate) mod tests;
