//! Port of `transport/internal/ipc/tcp.go` (DEV-518 W8, slice 10): the IPC over TCP for the
//! standalone container. The Unix socket stays the only transport HA's own subprocess uses;
//! nothing here is reachable unless a caller asks for [`Server::listen_tcp`].
//!
//! The Unix socket is safe because it is mode 0600 and spawned by HA itself. A TCP port has
//! neither property, and a connection is FULL control (configure any driver, `open_relay`,
//! `house_open` ...), so this transport is closed in layers, exactly as in Go:
//!
//! 1. a token is mandatory (at least [`MIN_TOKEN_LEN`] bytes): the first line a client sends must
//!    be `{"id":N,"method":"auth","params":{"token":"..."}}`, within [`AUTH_WINDOW`] (the whole
//!    first line, not an idle timer), compared in constant time; anything else drops the
//!    connection before it is served or can receive a notification;
//! 2. loopback unless `allow_non_loopback` is set, which in turn refuses to run without an
//!    allow-from list (the same two-flag rule as `gate_sip_server`);
//! 3. a connection from outside the allow-from list is closed before a byte is read, and before
//!    it can take an admission slot (R2-10).
//!
//! On top of Go's three (PARITY 302, 400): connections are checked on their own tasks, the newest
//! one that proves the token replaces the one being served, and a source that keeps sending wrong
//! tokens is slowed down on its FAILURE path only: a right token is never refused or delayed.
//!
//! The stream is NOT encrypted: carry it over loopback, a private docker network, or a tunnel
//! you trust. A wrong token is never logged with its value, and the token is kept out of every
//! `Debug` output.

use crate::util::lock;
use std::collections::{HashMap, VecDeque};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, ToSocketAddrs};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use tokio::io::AsyncWriteExt;
use tokio::net::TcpListener;
use tokio::sync::{oneshot, Notify};
use tokio::time::{timeout, timeout_at, Instant};

use super::{
    parse_object_entries, response_line, BoxRead, BoxWrite, IpcError, LineScanner, Listener, Reply,
    Scan, Server,
};
use crate::gojson;
use crate::gostd::net::split_host_port;

/// The shortest accepted shared secret (`MinTokenLen`), in bytes.
pub const MIN_TOKEN_LEN: usize = 24;

/// How long a new connection has to send its whole `auth` line (`authWindow`).
pub const AUTH_WINDOW: Duration = Duration::from_secs(5);

/// A masked network prefix (`netip.Prefix`). A prefix of one family never contains an address of
/// the other: an IPv4-mapped IPv6 prefix (`::ffff:10.0.0.0/104`) matches nothing, because the
/// peer address is un-mapped before the test, exactly as in Go.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Prefix {
    addr: IpAddr,
    bits: u8,
}

impl Prefix {
    /// `netip.PrefixFrom(addr, bits).Masked()`. `bits` is at most 32 (IPv4) or 128 (IPv6).
    pub(crate) fn new(addr: IpAddr, bits: u8) -> Prefix {
        assert!(
            bits <= if addr.is_ipv4() { 32 } else { 128 },
            "prefix length {bits} too large"
        );
        let addr = match addr {
            IpAddr::V4(a) => {
                let mask = if bits == 0 {
                    0
                } else {
                    u32::MAX << (32 - bits as u32)
                };
                IpAddr::V4(Ipv4Addr::from(u32::from(a) & mask))
            }
            IpAddr::V6(a) => {
                let mask = if bits == 0 {
                    0
                } else {
                    u128::MAX << (128 - bits as u32)
                };
                IpAddr::V6(Ipv6Addr::from(u128::from(a) & mask))
            }
        };
        Prefix { addr, bits }
    }

    pub fn bits(&self) -> u8 {
        self.bits
    }

    /// `netip.Prefix.Contains`.
    pub fn contains(&self, ip: IpAddr) -> bool {
        match (self.addr, ip) {
            (IpAddr::V4(p), IpAddr::V4(a)) => {
                let mask = if self.bits == 0 {
                    0
                } else {
                    u32::MAX << (32 - self.bits as u32)
                };
                u32::from(p) == u32::from(a) & mask
            }
            (IpAddr::V6(p), IpAddr::V6(a)) => {
                let mask = if self.bits == 0 {
                    0
                } else {
                    u128::MAX << (128 - self.bits as u32)
                };
                u128::from(p) == u128::from(a) & mask
            }
            _ => false,
        }
    }
}

impl std::fmt::Display for Prefix {
    /// `netip.Prefix.String()`: Go prints an IPv4-mapped prefix as `::ffff:a.b.c.d/N`, which is
    /// what Rust's `Ipv6Addr` Display also does.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}/{}", self.addr, self.bits)
    }
}

/// `TCPOptions`. `Debug` hides the token.
#[derive(Clone, Default)]
pub struct TcpOptions {
    pub token: String,
    pub allow_non_loopback: bool,
    pub allow_from: Vec<Prefix>,
}

impl std::fmt::Debug for TcpOptions {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TcpOptions")
            .field("token", &"<redacted>")
            .field("allow_non_loopback", &self.allow_non_loopback)
            .field("allow_from", &self.allow_from)
            .finish()
    }
}

/// `tcpGate`: the checks every TCP connection passes before it is served.
pub(super) struct TcpGate {
    token: Vec<u8>,
    allow_from: Vec<Prefix>,
    refusals: StdMutex<RateLimit>,
    /// R1-37: the connections waiting for their `auth` line, oldest first (at most
    /// [`ADMISSION_SLOTS`]; a newcomer beyond that drops the oldest, R2-11).
    pending: Arc<StdMutex<Pending>>,
    /// wrong-token replies being held back (at most [`MAX_DELAYED`])
    delayed: Arc<AtomicUsize>,
    /// the newest authenticated connection, waiting for `serve` to take it (it replaces the
    /// current one: it has proved it is the hub)
    ready: StdMutex<Option<Admitted>>,
    ready_sig: Notify,
    /// sources that sent a wrong token, and how many times lately
    strikes: StdMutex<HashMap<IpAddr, Strikes>>,
    /// the task that accepts connections and checks them (started by the first `serve`)
    acceptor: StdMutex<Option<tokio::task::JoinHandle<()>>>,
}

/// R1-37: connections checked at the same time.
const ADMISSION_SLOTS: usize = 4;
/// R2-11: wrong tokens from one source within [`STRIKE_WINDOW`] before its failures are slowed ...
const BAD_TOKEN_STRIKES: u32 = 3;
const STRIKE_WINDOW: Duration = Duration::from_secs(10);
/// ... by this much: the `unauthorized` reply and the close come this late. The source is still
/// READ, and a right token is served at once.
const BAD_TOKEN_BACKOFF: Duration = Duration::from_secs(5);
/// R2-11: how many slowed replies are held at once; the rest are closed without a reply, so
/// slowed failures cannot pile up (a held reply costs a task and a socket, not an admission slot).
const MAX_DELAYED: usize = 8;
const BACKOFF_MAX_SOURCES: usize = 64;

struct Strikes {
    count: u32,
    first: std::time::Instant,
}

/// Why a connection was not admitted. `Display` is the log text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Refusal {
    /// outside `-ipc-allow-from`
    Source,
    /// no valid `auth` line (garbage, another method, too long, a timeout)
    NoLine,
    BadToken,
    /// dropped to make room: the oldest of too many connections waiting to authenticate
    Evicted,
}

impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Refusal::Source => "source not allowed",
            Refusal::NoLine => "no valid auth line",
            Refusal::BadToken => "bad token",
            Refusal::Evicted => {
                "too many connections are waiting to authenticate (the oldest was dropped)"
            }
        })
    }
}

#[derive(Default)]
struct Pending {
    next: u64,
    waiting: VecDeque<(u64, oneshot::Sender<()>)>,
}

/// A place among the connections waiting to authenticate; freed when dropped.
pub(super) struct PendingGuard {
    id: u64,
    pending: Arc<StdMutex<Pending>>,
}

impl Drop for PendingGuard {
    fn drop(&mut self) {
        lock(&self.pending).waiting.retain(|(id, _)| *id != self.id);
    }
}

/// A held-back reply; counted while it lives.
struct DelayedGuard(Arc<AtomicUsize>);

impl Drop for DelayedGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

/// A connection that passed the gate: what `serve` needs to run it.
pub(super) struct Admitted {
    pub(super) rd: BoxRead,
    pub(super) wr: BoxWrite,
    /// whatever the client sent after its auth line
    pub(super) buf: Vec<u8>,
}

impl TcpGate {
    pub(super) fn new(token: Vec<u8>, allow_from: Vec<Prefix>) -> TcpGate {
        TcpGate {
            token,
            allow_from,
            refusals: Default::default(),
            pending: Default::default(),
            delayed: Default::default(),
            ready: StdMutex::new(None),
            ready_sig: Notify::new(),
            strikes: Default::default(),
            acceptor: StdMutex::new(None),
        }
    }

    /// The authenticated connection `serve` is to run next, waiting for one if need be.
    pub(super) async fn next_admitted(&self) -> Admitted {
        loop {
            if let Some(a) = lock(&self.ready).take() {
                return a;
            }
            self.ready_sig.notified().await;
        }
    }

    /// Resolves when an authenticated connection is waiting (leaves it in place).
    pub(super) async fn newcomer(&self) {
        loop {
            if lock(&self.ready).is_some() {
                return;
            }
            self.ready_sig.notified().await;
        }
    }

    fn offer(&self, a: Admitted) {
        // an older waiting connection is dropped (closed): the newest authenticated one wins
        *lock(&self.ready) = Some(a);
        self.ready_sig.notify_waiters();
        self.ready_sig.notify_one();
    }

    /// Takes a place among the connections waiting to authenticate. When all
    /// [`ADMISSION_SLOTS`] are taken the OLDEST waiting connection is dropped to make room (R2-11):
    /// the newcomer is never the one refused, and a connection that has been silent longest is the
    /// least likely to be the hub. The receiver resolves when this connection is the one dropped.
    fn enter(&self) -> (PendingGuard, oneshot::Receiver<()>) {
        let (tx, rx) = oneshot::channel();
        let mut p = lock(&self.pending);
        while p.waiting.len() >= ADMISSION_SLOTS {
            if let Some((_, old)) = p.waiting.pop_front() {
                let _ = old.send(());
            }
        }
        p.next += 1;
        let id = p.next;
        p.waiting.push_back((id, tx));
        (
            PendingGuard {
                id,
                pending: self.pending.clone(),
            },
            rx,
        )
    }

    /// A wrong token from `ip`. How long its failure is to be held back: nothing for the first
    /// two within [`STRIKE_WINDOW`] (one typo, or a token fixed a moment ago, costs nothing),
    /// [`BAD_TOKEN_BACKOFF`] from the third on. Only the wrong-token reply is ever slowed.
    fn strike(&self, ip: IpAddr) -> Duration {
        let mut m = lock(&self.strikes);
        let now = std::time::Instant::now();
        if m.len() >= BACKOFF_MAX_SOURCES {
            m.retain(|_, st| now.duration_since(st.first) < STRIKE_WINDOW);
        }
        if m.len() >= BACKOFF_MAX_SOURCES && !m.contains_key(&ip) {
            return Duration::ZERO;
        }
        let st = m.entry(ip).or_insert(Strikes {
            count: 0,
            first: now,
        });
        if now.duration_since(st.first) >= STRIKE_WINDOW {
            *st = Strikes {
                count: 0,
                first: now,
            };
        }
        st.count += 1;
        if st.count >= BAD_TOKEN_STRIKES {
            BAD_TOKEN_BACKOFF
        } else {
            Duration::ZERO
        }
    }

    fn delay_slot(&self) -> Option<DelayedGuard> {
        if self.delayed.fetch_add(1, Ordering::SeqCst) >= MAX_DELAYED {
            self.delayed.fetch_sub(1, Ordering::SeqCst);
            return None;
        }
        Some(DelayedGuard(self.delayed.clone()))
    }

    /// Stops the accepting task (so the listener it holds is dropped before `close` returns).
    pub(super) async fn stop_acceptor(&self) {
        let h = lock(&self.acceptor).take();
        if let Some(h) = h {
            h.abort();
            let _ = h.await;
        }
        lock(&self.ready).take();
    }
}

impl std::fmt::Debug for TcpGate {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TcpGate")
            .field("token", &"<redacted>")
            .field("allow_from", &self.allow_from)
            .finish()
    }
}

/// At most one log line per 10 s for refused connections; the rest are counted into the next line.
#[derive(Default)]
pub(super) struct RateLimit {
    last: Option<std::time::Instant>,
    suppressed: u32,
}

impl RateLimit {
    pub(super) const EVERY: Duration = Duration::from_secs(10);

    /// `Some(n)`: log now, `n` refusals were not logged since the last line. `None`: stay quiet.
    pub(super) fn allow(&mut self, now: std::time::Instant) -> Option<u32> {
        match self.last {
            Some(t) if now.duration_since(t) < Self::EVERY => {
                self.suppressed = self.suppressed.saturating_add(1);
                None
            }
            _ => {
                self.last = Some(now);
                Some(std::mem::take(&mut self.suppressed))
            }
        }
    }
}

/// `subtle.ConstantTimeCompare`: false at once when the lengths differ (the length of the
/// token is not a secret worth hiding, as in Go), otherwise a comparison whose time does not
/// depend on where the first difference is.
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b) {
        diff |= x ^ y;
    }
    std::hint::black_box(diff) == 0
}

/// `net.IP.IsLoopback`: 127.0.0.0/8 (an IPv4-mapped IPv6 address counts as the IPv4 one) or `::1`.
fn is_loopback(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(a) => a.octets()[0] == 127,
        IpAddr::V6(a) => match a.to_ipv4_mapped() {
            Some(v4) => v4.octets()[0] == 127,
            None => a == Ipv6Addr::LOCALHOST,
        },
    }
}

/// What `net.ResolveTCPAddr("tcp", addr)` returns that this code uses: the IP (`None` for an
/// empty host, the wildcard) and the port. Errors carry Go's texts for the address syntax and the
/// port; a port that is not a number is always `unknown port` (Go would also look a well-known
/// service name such as `smtp` up in `/etc/services`: PARITY difference 177).
fn resolve_tcp_addr(addr: &str) -> Result<(Option<IpAddr>, u16), String> {
    let (host, port) = split_host_port(addr)?;
    let port: u16 = if port.is_empty() {
        0
    } else {
        // Go accepts a decimal number with an optional sign; anything else is a service name
        let (neg, digits) = match port.as_bytes()[0] {
            b'-' => (true, &port[1..]),
            b'+' => (false, &port[1..]),
            _ => (false, port),
        };
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return Err(format!("lookup tcp/{port}: unknown port"));
        }
        let n = digits
            .bytes()
            .fold(0u64, |n, b| (n * 10 + (b - b'0') as u64).min(1 << 40));
        if (neg && n != 0) || n > 65535 {
            return Err(format!("address {port}: invalid port"));
        }
        n as u16
    };
    if host.is_empty() {
        return Ok((None, port));
    }
    if let Ok(ip) = host.parse::<IpAddr>() {
        return Ok((Some(ip), port));
    }
    // a host name: Go's resolver for "tcp" prefers an IPv4 address
    let found: Vec<SocketAddr> = (host, port)
        .to_socket_addrs()
        .map_err(|_| format!("lookup {host}: no such host"))?
        .collect();
    let pick = found
        .iter()
        .find(|a| a.is_ipv4())
        .or_else(|| found.first())
        .ok_or_else(|| format!("lookup {host}: no such host"))?;
    Ok((Some(pick.ip()), port))
}

/// What Go's `net` does to every accepted TCP connection: `TCP_NODELAY` (the small, latency-bound
/// notifications must not wait for an ACK) and keep-alive probes after 15 s idle, every 15 s, 9
/// times. The server serves ONE connection at a time, so a hub host that vanished without a FIN
/// would otherwise hold the slot until the next write failed.
pub(super) fn tune(stream: &tokio::net::TcpStream) {
    let _ = stream.set_nodelay(true);
    let ka = socket2::TcpKeepalive::new()
        .with_time(Duration::from_secs(15))
        .with_interval(Duration::from_secs(15))
        .with_retries(9);
    let _ = socket2::SockRef::from(stream).set_tcp_keepalive(&ka);
}

/// A listening socket with the backlog of 1024 (`v6_dual`: `IPV6_V6ONLY` off, so IPv4 clients
/// arrive as mapped addresses).
fn bind_listener(sa: SocketAddr, v6_dual: bool) -> std::io::Result<TcpListener> {
    let domain = if sa.is_ipv4() {
        socket2::Domain::IPV4
    } else {
        socket2::Domain::IPV6
    };
    let s = socket2::Socket::new(domain, socket2::Type::STREAM, None)?;
    if v6_dual {
        s.set_only_v6(false)?;
    }
    s.set_reuse_address(true)?;
    s.bind(&sa.into())?;
    s.listen(1024)?;
    s.set_nonblocking(true)?;
    TcpListener::from_std(s.into())
}

/// `[::]:port` with `IPV6_V6ONLY` off, so IPv4 clients arrive as mapped addresses.
fn bind_wildcard_v6(port: u16) -> Result<TcpListener, (SocketAddr, std::io::Error)> {
    let sa = SocketAddr::new(IpAddr::V6(Ipv6Addr::UNSPECIFIED), port);
    bind_listener(sa, true).map_err(|e| (sa, e))
}

impl Server {
    /// `ListenTCP`: listens on `addr` (`host:port`) for the one HA hub connection. Must be called
    /// inside a tokio runtime. A host name is resolved HERE, once, and the listener is bound to
    /// the address that was checked (Go resolves it a second time inside `net.Listen`; PARITY
    /// difference 178).
    pub fn listen_tcp(addr: &str, o: TcpOptions) -> Result<Server, String> {
        if o.token.len() < MIN_TOKEN_LEN {
            return Err(format!(
                "ipc: the TCP listener needs a token of at least {MIN_TOKEN_LEN} characters"
            ));
        }
        let (ip, port) = resolve_tcp_addr(addr).map_err(|e| {
            format!(
                "ipc: bad listen address {}: {e}",
                crate::gostd::strconv::go_quote(addr)
            )
        })?;
        if ip.map(is_loopback) != Some(true) {
            if !o.allow_non_loopback {
                return Err(format!(
                    "ipc: refusing to listen on {}: loopback only until the non-loopback flag is set",
                    crate::gostd::strconv::go_quote(addr)
                ));
            }
            if o.allow_from.is_empty() {
                return Err("ipc: a non-loopback listen address needs an allow-from list".into());
            }
        }
        // one helper for the explicit and the wildcard address (R1-73: the backlog was 128 for
        // one and 1024 for the other)
        let bind = |sa: SocketAddr| -> std::io::Result<TcpListener> { bind_listener(sa, false) };
        let bound = match ip {
            Some(ip) => bind(SocketAddr::new(ip, port)).map_err(|e| (SocketAddr::new(ip, port), e)),
            // the wildcard: dual-stack when the host has IPv6 (IPV6_V6ONLY off, which Go forces
            // rather than leaving to the sysctl), else IPv4 only; a double failure reports the
            // IPv4 one
            None => bind_wildcard_v6(port).or_else(|_| {
                let v4 = SocketAddr::new(IpAddr::V4(Ipv4Addr::UNSPECIFIED), port);
                bind(v4).map_err(|e| (v4, e))
            }),
        };
        let l = bound.map_err(|(sa, e)| {
            format!(
                "ipc: listening on {addr}: listen tcp {sa}: bind: {}",
                gojson::io_err(&e)
            )
        })?;
        let local = l
            .local_addr()
            .map_err(|e| format!("ipc: listening on {addr}: {}", gojson::io_err(&e)))?;
        Ok(Server::new(Listener::Tcp {
            l: StdMutex::new(Some(Arc::new(l))),
            gate: Box::new(TcpGate::new(o.token.into_bytes(), o.allow_from)),
            local,
        }))
    }

    /// `Addr`: the TCP listener's bound address (tests bind port 0). `None` on a Unix server.
    pub fn tcp_addr(&self) -> Option<SocketAddr> {
        match &self.0.listener {
            Listener::Tcp { local, .. } => Some(*local),
            Listener::Unix { .. } => None,
        }
    }
}

/// One `auth` request line, decoded as Go's `json.Unmarshal` does into
/// `struct{ID int; Method string; Params struct{Token string}}`.
struct AuthReq {
    id: i64,
    method: String,
    token: String,
}

/// `None` is Go's "Unmarshal failed": the connection is dropped without a word. Keys match
/// exactly, else case-insensitively; a `null` member is skipped; for duplicate keys the last
/// wins, and two `params` objects are merged field by field (Go decodes the second into the
/// struct the first filled).
fn parse_auth(line: &[u8]) -> Option<AuthReq> {
    let find = |names: &[&str], key: &str| -> Option<usize> {
        names
            .iter()
            .position(|n| *n == key)
            .or_else(|| names.iter().position(|n| n.eq_ignore_ascii_case(key)))
    };
    let mut req = AuthReq {
        id: 0,
        method: String::new(),
        token: String::new(),
    };
    for (key, val) in parse_object_entries(line)? {
        let text = val.get();
        match find(&["id", "method", "params"], &key) {
            Some(0) if text != "null" => req.id = text.parse::<i64>().ok()?,
            Some(1) if text != "null" => req.method = serde_json::from_str::<String>(text).ok()?,
            Some(2) if text != "null" => {
                for (k2, v2) in parse_object_entries(text.as_bytes())? {
                    if find(&["token"], &k2).is_some() && v2.get() != "null" {
                        req.token = serde_json::from_str::<String>(v2.get()).ok()?;
                    }
                }
            }
            _ => {}
        }
    }
    Some(req)
}

impl TcpGate {
    /// `fromAllowed`: an empty list allows everyone; otherwise the peer (IPv4-mapped IPv6
    /// un-mapped) must be inside one prefix. A peer whose address cannot be read is refused.
    pub(super) fn source_allowed(&self, peer: Option<SocketAddr>) -> bool {
        if self.allow_from.is_empty() {
            return true;
        }
        let Some(peer) = peer else {
            return false;
        };
        let ip = match peer.ip() {
            IpAddr::V6(a) => a.to_ipv4_mapped().map(IpAddr::V4).unwrap_or(IpAddr::V6(a)),
            v4 => v4,
        };
        self.allow_from.iter().any(|p| p.contains(ip))
    }

    /// `admit`: the source check and the auth handshake. It writes its one reply straight to
    /// `wr` (the connection is not yet the server's current one) and ignores a failed write, as
    /// Go does: the answer is the same whether or not the peer is still listening. Whatever the
    /// client sent after the auth line stays in `buf` for the read loop.
    ///
    /// A wrong token from a source that has sent several lately is answered LATE (R2-11): the
    /// admission slot in `slot` is given up first, then the reply and the close wait. The source
    /// is read like any other and a right token is never held back.
    async fn admit_inner(
        &self,
        rd: &mut BoxRead,
        wr: &mut BoxWrite,
        peer: Option<SocketAddr>,
        buf: &mut Vec<u8>,
        write_timeout: Duration,
        slot: &mut Option<PendingGuard>,
    ) -> Result<(), Refusal> {
        if !self.source_allowed(peer) {
            return Err(Refusal::Source);
        }
        let deadline = Instant::now() + AUTH_WINDOW;
        let mut chunk = vec![0u8; 16 * 1024];
        let mut lines = LineScanner::with_buffered(std::mem::take(buf));
        let line = loop {
            match lines.next_line() {
                Scan::Line(l) => break l,
                Scan::TooLong => return Err(Refusal::NoLine),
                Scan::More => {}
            }
            match timeout_at(deadline, tokio::io::AsyncReadExt::read(rd, &mut chunk)).await {
                Ok(Ok(0)) => {
                    // a final line with no newline is still a token
                    match lines.finish() {
                        Some(l) => break l,
                        None => return Err(Refusal::NoLine),
                    }
                }
                Ok(Ok(n)) => lines.push(&chunk[..n]),
                // a read error, or the window ran out (Go would hand out the partial line here;
                // PARITY 401)
                Ok(Err(_)) | Err(_) => return Err(Refusal::NoLine),
            }
        };
        *buf = lines.take_rest();
        let Some(req) = parse_auth(&line) else {
            return Err(Refusal::NoLine);
        };
        if req.method != "auth" {
            return Err(Refusal::NoLine);
        }
        let ok = constant_time_eq(req.token.as_bytes(), &self.token);
        let mut _held = None;
        if !ok {
            let wait = peer.map_or(Duration::ZERO, |p| self.strike(p.ip()));
            if !wait.is_zero() {
                slot.take(); // a held reply does not hold an admission slot
                let Some(h) = self.delay_slot() else {
                    return Err(Refusal::BadToken); // too many held already: closed, no reply
                };
                _held = Some(h);
                tokio::time::sleep(wait).await;
            }
        }
        let reply: Result<Reply, IpcError> = if ok {
            Ok(Reply::Json(serde_json::json!({"ok": true})))
        } else {
            Err(IpcError::new("unauthorized", "bad token"))
        };
        let text = response_line(req.id, &reply);
        let _ = timeout(write_timeout, wr.write_all(text.as_bytes())).await;
        if ok {
            Ok(())
        } else {
            Err(Refusal::BadToken)
        }
    }

    /// `admit`: see [`Self::admit_inner`]; a refusal is logged (rate-limited, the peer address
    /// only: never the token, never a byte of the line).
    pub(super) async fn admit(
        &self,
        rd: &mut BoxRead,
        wr: &mut BoxWrite,
        peer: Option<SocketAddr>,
        buf: &mut Vec<u8>,
        write_timeout: Duration,
        slot: &mut Option<PendingGuard>,
    ) -> Result<(), Refusal> {
        let r = self
            .admit_inner(rd, wr, peer, buf, write_timeout, slot)
            .await;
        if let Err(reason) = r {
            self.log_refusal(peer, reason);
        }
        r
    }

    /// The refusal line: at most one per 10 s, the peer address only (never the token, never a
    /// byte of the line).
    fn log_refusal(&self, peer: Option<SocketAddr>, reason: Refusal) {
        let n = lock(&self.refusals).allow(std::time::Instant::now());
        if let Some(n) = n {
            let from = peer
                .map(|p| p.ip().to_string())
                .unwrap_or_else(|| "?".into());
            let more = if n > 0 {
                format!(" (and {n} more since the last line)")
            } else {
                String::new()
            };
            crate::panic_hook::err_line(&format!(
                "portico-transport: ipc: refused a TCP connection from {from}: {reason}{more}"
            ));
        }
    }
}

impl Server {
    /// Starts the task that accepts and checks connections, unless it is running (R1-37). It
    /// runs while a connection is being served too, so a restarted hub that presents the right
    /// token is read at once and replaces a half-open predecessor.
    pub(super) fn ensure_acceptor(&self) {
        let Listener::Tcp { l, gate, .. } = &self.0.listener else {
            return;
        };
        let mut h = lock(&gate.acceptor);
        if h.as_ref().is_some_and(|j| !j.is_finished()) || self.is_closed() {
            return;
        }
        let Some(listener) = lock(l).clone() else {
            return;
        };
        let me = self.clone();
        *h = Some(tokio::spawn(async move {
            loop {
                match listener.accept().await {
                    Ok((s, peer)) => me.admit_in_task(s, peer),
                    Err(e) => {
                        if me.is_closed() {
                            return;
                        }
                        crate::panic_hook::err_line(&format!(
                            "portico-transport: ipc: accept: {} (accepting again)",
                            gojson::io_err(&e)
                        ));
                        tokio::time::sleep(Duration::from_secs(1)).await;
                    }
                }
            }
        }));
    }

    /// Checks one accepted connection on its own task (at most [`ADMISSION_SLOTS`] waiting at a
    /// time: a newcomer beyond that drops the oldest); a connection that passes is offered to
    /// `serve`. The source is checked FIRST (R2-10): a peer outside the allow-from list is closed
    /// at once and never takes a place from the hub.
    fn admit_in_task(&self, s: tokio::net::TcpStream, peer: SocketAddr) {
        let Listener::Tcp { gate, .. } = &self.0.listener else {
            return;
        };
        if !gate.source_allowed(Some(peer)) {
            gate.log_refusal(Some(peer), Refusal::Source);
            return;
        }
        tune(&s);
        let (guard, mut evicted) = gate.enter();
        let me = self.clone();
        let mut close_rx = self.0.close_tx.subscribe();
        tokio::spawn(async move {
            let Listener::Tcp { gate, .. } = &me.0.listener else {
                return;
            };
            let mut slot = Some(guard);
            let (r, w) = s.into_split();
            let (mut rd, mut wr): (BoxRead, BoxWrite) = (Box::new(r), Box::new(w));
            let mut buf = Vec::new();
            let wt = *lock(&me.0.write_timeout);
            // biased, and `admit` BEFORE the eviction (R3A-1): a connection whose line is already in
            // the socket is read even when a newcomer took its place before this task first ran
            // (Go admits it: it is first in the accept queue); one that has sent nothing is what
            // the eviction drops. The answer does not depend on a coin toss.
            let verdict = tokio::select! {
                biased;
                _ = close_rx.wait_for(|c| *c) => return,
                v = gate.admit(&mut rd, &mut wr, Some(peer), &mut buf, wt, &mut slot) => v,
                // `Err`: the sender went away because the place was given up: not an eviction
                Ok(()) = &mut evicted => {
                    gate.log_refusal(Some(peer), Refusal::Evicted);
                    return;
                }
            };
            if verdict.is_ok() {
                gate.offer(Admitted { rd, wr, buf });
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ip(s: &str) -> IpAddr {
        s.parse().unwrap()
    }

    #[test]
    fn prefixes_are_masked_and_never_cross_families() {
        let p = Prefix::new(ip("10.8.7.9"), 16);
        assert_eq!(p.to_string(), "10.8.0.0/16");
        assert!(p.contains(ip("10.8.255.255")));
        assert!(!p.contains(ip("10.3.0.1")));
        // a v4-mapped v6 address does not match a v4 prefix, and a v4 address no v6 prefix
        assert!(!p.contains(ip("::ffff:10.8.0.1")));
        let v6 = Prefix::new(ip("fd00:1:2::5"), 48);
        assert_eq!(v6.to_string(), "fd00:1:2::/48");
        assert!(v6.contains(ip("fd00:1:2:ffff::1")));
        assert!(!v6.contains(ip("10.8.0.1")));
        // /32 and /128 are single hosts
        assert!(Prefix::new(ip("1.2.3.4"), 32).contains(ip("1.2.3.4")));
        assert!(!Prefix::new(ip("1.2.3.4"), 32).contains(ip("1.2.3.5")));
    }

    #[test]
    fn a_mapped_peer_is_tested_as_its_v4_address() {
        let gate = TcpGate::new(b"x".to_vec(), vec![Prefix::new(ip("10.8.0.0"), 16)]);
        let mapped: SocketAddr = "[::ffff:10.8.0.9]:5000".parse().unwrap();
        let outside: SocketAddr = "[::ffff:10.3.0.9]:5000".parse().unwrap();
        let v6: SocketAddr = "[fd00::1]:5000".parse().unwrap();
        assert!(gate.source_allowed(Some(mapped)));
        assert!(!gate.source_allowed(Some(outside)));
        assert!(!gate.source_allowed(Some(v6)));
        // an unreadable peer address is refused when there is a list, accepted when there is none
        assert!(!gate.source_allowed(None));
        let open = TcpGate::new(b"x".to_vec(), vec![]);
        assert!(open.source_allowed(None));
    }

    #[test]
    fn loopback_is_127_slash_8_and_the_v6_one_including_mapped() {
        for a in ["127.0.0.1", "127.9.9.9", "::1", "::ffff:127.0.0.1"] {
            assert!(is_loopback(ip(a)), "{a}");
        }
        for a in [
            "0.0.0.0",
            "10.0.0.1",
            "::",
            "::ffff:10.0.0.1",
            "fe80::1",
            "128.0.0.1",
        ] {
            assert!(!is_loopback(ip(a)), "{a}");
        }
    }

    #[test]
    fn split_host_port_has_gos_texts() {
        assert_eq!(split_host_port("a:1"), Ok(("a", "1")));
        assert_eq!(split_host_port("[::1]:80"), Ok(("::1", "80")));
        assert_eq!(split_host_port(":80"), Ok(("", "80")));
        for (input, why) in [
            ("a", "missing port in address"),
            ("[::1]", "missing port in address"),
            ("[::1]x:80", "missing port in address"),
            ("[::1", "missing ']' in address"),
            ("a:b:c", "too many colons in address"),
            ("[::1]:80:1", "too many colons in address"),
            ("a[:80", "unexpected '[' in address"),
            ("a]:80", "unexpected ']' in address"),
        ] {
            assert_eq!(
                split_host_port(input),
                Err(format!("address {input}: {why}")),
                "{input}"
            );
        }
    }

    #[test]
    fn ports_are_numbers_with_an_optional_sign() {
        assert_eq!(
            resolve_tcp_addr("127.0.0.1:"),
            Ok((Some(ip("127.0.0.1")), 0))
        );
        assert_eq!(
            resolve_tcp_addr("127.0.0.1:+80"),
            Ok((Some(ip("127.0.0.1")), 80))
        );
        assert_eq!(
            resolve_tcp_addr("127.0.0.1:-0"),
            Ok((Some(ip("127.0.0.1")), 0))
        );
        assert_eq!(resolve_tcp_addr(":65535"), Ok((None, 65535)));
        assert_eq!(
            resolve_tcp_addr("127.0.0.1:65536"),
            Err("address 65536: invalid port".into())
        );
        assert_eq!(
            resolve_tcp_addr("127.0.0.1:-5"),
            Err("address -5: invalid port".into())
        );
        // a service name is never looked up (PARITY difference 177)
        assert_eq!(
            resolve_tcp_addr("127.0.0.1:smtp"),
            Err("lookup tcp/smtp: unknown port".into())
        );
        assert_eq!(
            resolve_tcp_addr("127.0.0.1:+"),
            Err("lookup tcp/+: unknown port".into())
        );
    }

    #[test]
    fn the_token_comparison_is_exact() {
        assert!(constant_time_eq(b"abc", b"abc"));
        assert!(!constant_time_eq(b"abc", b"abd"));
        assert!(!constant_time_eq(b"abc", b"abcd"));
        assert!(!constant_time_eq(b"", b"a"));
        assert!(constant_time_eq(b"", b""));
        // a difference in the first, the middle and the last byte are each found
        let a = [7u8; 40];
        for i in [0, 20, 39] {
            let mut b = a;
            b[i] ^= 1;
            assert!(!constant_time_eq(&a, &b), "byte {i}");
        }
    }

    #[test]
    fn auth_lines_decode_as_gos_unmarshal_does() {
        let ok = |l: &str| parse_auth(l.as_bytes()).map(|r| (r.id, r.method, r.token));
        assert_eq!(
            ok(r#"{"id":7,"method":"auth","params":{"token":"t"}}"#),
            Some((7, "auth".into(), "t".into()))
        );
        // case-insensitive keys, last duplicate wins, params merge, null skipped
        assert_eq!(
            ok(
                r#"{"ID":1,"Method":"x","method":"auth","PARAMS":{"TOKEN":"a"},"params":{"y":1},"params":null}"#
            ),
            Some((1, "auth".into(), "a".into()))
        );
        assert_eq!(
            ok(r#"{"id":null,"method":null,"params":null}"#),
            Some((0, "".into(), "".into()))
        );
        for bad in [
            "",
            "null",
            "[]",
            r#""x""#,
            "{",
            r#"{"id":"1"}"#,
            r#"{"id":1.5}"#,
            r#"{"id":1e0}"#,
            r#"{"method":1}"#,
            r#"{"params":5}"#,
            r#"{"params":{"token":5}}"#,
            r#"{"id":1} x"#,
            r#"{"id":9223372036854775808}"#,
        ] {
            // (Go reads "null" as a no-op request with an empty method, which is refused all the
            // same; here it is not an object at all: pinned to `None`, R1-101)
            assert!(ok(bad).is_none(), "{bad:?}");
        }
    }

    #[tokio::test]
    async fn accepted_connections_get_nodelay_and_go_s_keepalive() {
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let c = tokio::net::TcpStream::connect(l.local_addr().unwrap())
            .await
            .unwrap();
        let (s, _) = l.accept().await.unwrap();
        assert!(!s.nodelay().unwrap());
        tune(&s);
        assert!(s.nodelay().unwrap());
        let r = socket2::SockRef::from(&s);
        assert!(r.keepalive().unwrap());
        assert_eq!(r.tcp_keepalive_time().unwrap(), Duration::from_secs(15));
        assert_eq!(r.tcp_keepalive_interval().unwrap(), Duration::from_secs(15));
        assert_eq!(r.tcp_keepalive_retries().unwrap(), 9);
        drop(c);
    }

    #[test]
    fn refusals_are_logged_at_most_once_per_ten_seconds() {
        use std::time::Instant;
        let mut r = RateLimit::default();
        let t0 = Instant::now();
        assert_eq!(r.allow(t0), Some(0));
        assert_eq!(r.allow(t0 + Duration::from_secs(1)), None);
        assert_eq!(r.allow(t0 + Duration::from_secs(9)), None);
        assert_eq!(r.allow(t0 + Duration::from_secs(10)), Some(2));
        assert_eq!(r.allow(t0 + Duration::from_secs(11)), None);
        assert_eq!(r.allow(t0 + Duration::from_secs(25)), Some(1));
    }

    #[test]
    #[should_panic(expected = "too large")]
    fn a_prefix_wider_than_its_family_is_a_bug() {
        let _ = Prefix::new(ip("10.0.0.0"), 33);
    }

    #[tokio::test]
    async fn the_wildcard_is_dual_stack_with_v6only_off() {
        match bind_wildcard_v6(0) {
            Ok(l) => {
                assert!(!socket2::SockRef::from(&l).only_v6().unwrap());
                let port = l.local_addr().unwrap().port();
                // an IPv4 client reaches it
                let c = tokio::net::TcpStream::connect(("127.0.0.1", port)).await;
                assert!(c.is_ok(), "{c:?}");
            }
            Err((_, e)) => eprintln!("(no IPv6 on this host: {e}; test skipped)"),
        }
    }

    #[tokio::test]
    async fn a_failed_wildcard_bind_reports_the_ipv4_error() {
        let taken = std::net::TcpListener::bind("0.0.0.0:0").unwrap();
        let port = taken.local_addr().unwrap().port();
        let e = Server::listen_tcp(
            &format!(":{port}"),
            TcpOptions {
                token: "0123456789abcdef0123456789abcdef".into(),
                allow_non_loopback: true,
                allow_from: vec![Prefix::new(ip("10.0.0.0"), 8)],
            },
        )
        .err()
        .expect("bound a taken port");
        assert!(
            e.contains(&format!("0.0.0.0:{port}")) && e.contains("address already in use"),
            "{e}"
        );
    }

    #[test]
    fn nothing_prints_the_token() {
        let o = TcpOptions {
            token: "the-secret-token-0123456789".into(),
            allow_non_loopback: false,
            allow_from: vec![],
        };
        assert!(!format!("{o:?}").contains("secret"));
        let g = TcpGate::new(b"the-secret-token-0123456789".to_vec(), vec![]);
        assert!(!format!("{g:?}").contains("secret"));
    }
}
