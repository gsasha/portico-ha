//! The observer's one request: `GET /health` over HTTP/1.1, plain or TLS.
//!
//! Go uses `net/http` with `CheckRedirect = ErrUseLastResponse`, no proxy, one idle connection
//! kept for 30 s, a context timeout covering the whole exchange, and a body read capped at
//! 64 KiB + 1. transport-rs has no HTTP client crate (the dependency list is exact-pinned and
//! this is the only HTTP traffic the binary makes), so this is the smallest client that does the
//! same: it never follows a redirect (a 3xx is just a status), reads at most `limit` body
//! bytes, keeps the connection when the response was fully read and framed, and retries once on
//! a fresh connection when a REUSED connection turns out to be dead before any response byte
//! (what `net/http` does for an idempotent request). An `https` URL is polled over rustls with
//! the system CA bundle (`gostd::tls`, the same loader and the same wording of a
//! certificate failure), the server name being the URL's host name, as Go's `http.Transport`.
//!
//! Connecting is `dial::connect_tcp` (Go's `dialParallel`: a 300 ms head start for the first
//! address family, the deadline shared between the addresses).
//!
//! Differences from Go's request, all recorded in PARITY.md: no `Accept-Encoding: gzip` (the
//! body is never decompressed here), and the `User-Agent` names this binary.

use crate::util::lock;
use std::net::SocketAddr;
use std::pin::Pin;
use std::sync::atomic::{AtomicU16, Ordering};
use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, ReadBuf};
use tokio::net::TcpStream;
use tokio::time::Instant;

use super::tls::{self, Tls};
use crate::gojson::io_err;
use crate::gostd::strconv::go_quote;

/// `Transport.maxHeaderResponseSize`: 10 MiB when `MaxResponseHeaderBytes` is unset.
const MAX_HEADER: usize = 10 << 20;
const IDLE_TIMEOUT: Duration = Duration::from_secs(30);
/// `textproto`'s `maxLineLength` for a chunk-size line.
const MAX_CHUNK_LINE: usize = 4096;

/// `net/http`'s decoration of an error that reaches the caller after response bytes arrived on
/// a connection it then marks broken (`mapRoundTripError`).
const BROKEN: &str = "net/http: HTTP/1.x transport connection broken: ";

pub const USER_AGENT: &str = concat!("portico-transport-rs/", env!("CARGO_PKG_VERSION"));

/// What came back: the status and at most `limit` body bytes.
#[derive(Debug)]
pub struct Fetched {
    pub status: u16,
    pub body: Vec<u8>,
}

/// A failed request. `Url` errors are the ones Go wraps in `*url.Error` (the reason Python sees
/// is `GET /health: <text>`); `Body` errors come from reading the body and reach the reason
/// bare, as Go's `io.ReadAll` error does.
#[derive(Debug, PartialEq, Eq)]
pub enum FetchError {
    Url(String),
    Body(String),
}

/// A connection: plain TCP or TLS over it, with the addresses its errors are worded with.
enum Stream {
    Plain(TcpStream),
    Tls(Box<tokio_rustls::client::TlsStream<TcpStream>>),
}

struct Conn {
    s: Stream,
    local: Option<SocketAddr>,
    peer: Option<SocketAddr>,
}

impl Conn {
    /// `net.OpError` wording: `read tcp 127.0.0.1:a->127.0.0.1:b: read: connection reset by peer`.
    fn net_err(&self, op: &str, e: &std::io::Error) -> String {
        let (l, p) = (
            self.local.map_or(String::new(), |a| a.to_string()),
            self.peer.map_or(String::new(), |a| a.to_string()),
        );
        format!("{op} tcp {l}->{p}: {op}: {}", io_err(e))
    }

    fn tcp(&self) -> &TcpStream {
        match &self.s {
            Stream::Plain(t) => t,
            Stream::Tls(t) => t.get_ref().0,
        }
    }
}

impl AsyncRead for Conn {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        match &mut self.s {
            Stream::Plain(t) => Pin::new(t).poll_read(cx, buf),
            Stream::Tls(t) => Pin::new(t.as_mut()).poll_read(cx, buf),
        }
    }
}

impl AsyncWrite for Conn {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<std::io::Result<usize>> {
        match &mut self.s {
            Stream::Plain(t) => Pin::new(t).poll_write(cx, buf),
            Stream::Tls(t) => Pin::new(t.as_mut()).poll_write(cx, buf),
        }
    }
    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        match &mut self.s {
            Stream::Plain(t) => Pin::new(t).poll_flush(cx),
            Stream::Tls(t) => Pin::new(t.as_mut()).poll_flush(cx),
        }
    }
    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        match &mut self.s {
            Stream::Plain(t) => Pin::new(t).poll_shutdown(cx),
            Stream::Tls(t) => Pin::new(t.as_mut()).poll_shutdown(cx),
        }
    }
}

struct Idle {
    conn: Conn,
    since: Instant,
}

/// What an `https` URL adds: the TLS configuration and the name the handshake asks for.
struct TlsSetup {
    tls: Arc<Tls>,
    server_name: String,
}

pub struct HttpClient {
    /// `host:port` to dial (the URL's hostname and port, 80 by default)
    dial: String,
    /// the `Host` header value (the URL's host, as written)
    host_header: String,
    path: String,
    tls: Option<TlsSetup>,
    idle: std::sync::Mutex<Option<Idle>>,
}

/// The text of a failed dial (R2-6). At or after the request deadline Go's `getConn` prefers the
/// request context's error over the dial error (golang.org/issue/16049): `context deadline
/// exceeded`. An earlier per-address timeout is `dial tcp a: i/o timeout` (Go's `mapErr` of the
/// deadline error is not wrapped in the connect syscall error); any other error names `connect`.
fn dial_failure(a: SocketAddr, e: &std::io::Error, now: Instant, deadline: Instant) -> String {
    if e.kind() == std::io::ErrorKind::TimedOut {
        if now >= deadline {
            "context deadline exceeded".to_string()
        } else {
            format!("dial tcp {a}: i/o timeout")
        }
    } else {
        format!("dial tcp {a}: connect: {}", io_err(e))
    }
}

/// `dial tcp: lookup <host>: no such host`, the host without its port.
fn no_such_host(dial: &str) -> FetchError {
    let host = dial.rsplit_once(':').map_or(dial, |(h, _)| h);
    FetchError::Url(format!("dial tcp: lookup {host}: no such host"))
}

impl HttpClient {
    pub fn new(dial: String, host_header: String, path: String) -> Self {
        HttpClient {
            dial,
            host_header,
            path,
            tls: None,
            idle: std::sync::Mutex::new(None),
        }
    }

    /// The same client over TLS: the handshake asks for `server_name` and verifies against
    /// `tls`' roots.
    pub fn with_tls(mut self, tls: Arc<Tls>, server_name: String) -> Self {
        self.tls = Some(TlsSetup { tls, server_name });
        self
    }

    /// `CloseIdleConnections`.
    pub fn close_idle(&self) {
        lock(&self.idle).take();
    }

    fn take_idle(&self) -> Option<Conn> {
        let i = lock(&self.idle).take()?;
        if i.since.elapsed() > IDLE_TIMEOUT {
            return None;
        }
        // a connection the server has closed shows up as readable with 0 bytes; stray bytes (a
        // TLS alert included) are equally disqualifying
        let mut probe = [0u8; 1];
        match i.conn.tcp().try_read(&mut probe) {
            Ok(_) => None,
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => Some(i.conn),
            Err(_) => None,
        }
    }

    /// One `GET`, within `timeout` as a whole (connect, send, headers and body).
    pub async fn get(&self, timeout: Duration, limit: usize) -> Result<Fetched, FetchError> {
        self.get_status(timeout, limit).await.1
    }

    /// [`get`](Self::get), also saying which status the response head carried when the request
    /// failed after it (0: no head was read): a caller that judges the status before the body
    /// (Go's guard does) can still do so when the body is cut off (R2-74).
    pub async fn get_status(
        &self,
        timeout: Duration,
        limit: usize,
    ) -> (u16, Result<Fetched, FetchError>) {
        // the status the head carried PLUS ONE: 0 is "no head was read", and a head whose code is
        // `000` (three digits, as Go accepts) must not read as that (R3A-12)
        let status = AtomicU16::new(0);
        let deadline = Instant::now() + timeout;
        let r = match tokio::time::timeout_at(deadline, self.get_inner(deadline, &status, limit))
            .await
        {
            Ok(r) => r,
            Err(_) => {
                let t = "context deadline exceeded".to_string();
                Err(if status.load(Ordering::Relaxed) != 0 {
                    FetchError::Body(t)
                } else {
                    FetchError::Url(t)
                })
            }
        };
        (status.load(Ordering::Relaxed).saturating_sub(1), r)
    }

    /// The connect half of [`dial`](Self::dial): `connect` makes one attempt at one address (real
    /// TCP in production; the tests of R3B-3 pass a fake), the failure is worded by
    /// [`dial_failure`] (R2-6).
    async fn connect_addrs<F, Fut>(
        &self,
        addrs: &[SocketAddr],
        deadline: Instant,
        connect: F,
    ) -> Result<tokio::net::TcpStream, FetchError>
    where
        F: Fn(SocketAddr) -> Fut,
        Fut: std::future::Future<Output = std::io::Result<tokio::net::TcpStream>>,
    {
        match super::dial::connect_any(addrs, deadline, connect).await {
            Ok(s) => Ok(s),
            Err((a, e)) => Err(FetchError::Url(dial_failure(
                a,
                &e,
                Instant::now(),
                deadline,
            ))),
        }
    }

    async fn dial(&self, deadline: Instant) -> Result<Conn, FetchError> {
        let addrs: Vec<SocketAddr> = match tokio::net::lookup_host(&self.dial).await {
            Ok(a) => a.collect(),
            Err(_) => return Err(no_such_host(&self.dial)),
        };
        if addrs.is_empty() {
            return Err(no_such_host(&self.dial));
        }
        let tcp = self
            .connect_addrs(&addrs, deadline, tokio::net::TcpStream::connect)
            .await?;
        let (local, peer) = (tcp.local_addr().ok(), tcp.peer_addr().ok());
        let Some(t) = &self.tls else {
            return Ok(Conn {
                s: Stream::Plain(tcp),
                local,
                peer,
            });
        };
        let connector = t
            .tls
            .connector()
            .await
            .map_err(|e| FetchError::Url(format!("tls: {e}")))?;
        let name =
            tls::server_name(&t.server_name).map_err(|e| FetchError::Url(format!("tls: {e}")))?;
        match connector.connect(name, tcp).await {
            Ok(s) => Ok(Conn {
                s: Stream::Tls(Box::new(s)),
                local,
                peer,
            }),
            Err(e) if tls::is_cert_error(&e) => Err(FetchError::Url(format!(
                "tls: failed to verify certificate: {}",
                tls::cert_error_text(&e)
            ))),
            Err(e) => Err(FetchError::Url(format!("tls: {}", io_err(&e)))),
        }
    }

    async fn get_inner(
        &self,
        deadline: Instant,
        status_seen: &AtomicU16,
        limit: usize,
    ) -> Result<Fetched, FetchError> {
        let req = format!(
            "GET {} HTTP/1.1\r\nHost: {}\r\nUser-Agent: {}\r\nAccept: application/json\r\n\r\n",
            self.path, self.host_header, USER_AGENT
        );
        let mut attempt = 0;
        'attempt: loop {
            attempt += 1;
            let (mut conn, reused) = match self.take_idle() {
                Some(c) => (c, true),
                None => (self.dial(deadline).await?, false),
            };
            if let Err(e) = conn.write_all(req.as_bytes()).await {
                if reused && attempt == 1 {
                    continue 'attempt;
                }
                return Err(FetchError::Url(conn.net_err("write", &e)));
            }
            let mut buf: Vec<u8> = Vec::new();
            // how much of `buf` the head search has already looked at (R1-30: not again)
            let mut scanned = 0usize;
            let (parsed, mut rest) = loop {
                // headers (an interim 1xx response is followed by the real one)
                let head = loop {
                    if let Some(end) = find_head_end_from(&buf, scanned) {
                        break end;
                    }
                    scanned = buf.len();
                    if buf.len() > MAX_HEADER {
                        return Err(FetchError::Url(format!(
                            "{BROKEN}net/http: server response headers exceeded {MAX_HEADER} bytes; aborted"
                        )));
                    }
                    let mut tmp = [0u8; 4096];
                    match conn.read(&mut tmp).await {
                        Ok(0) => {
                            if buf.is_empty() {
                                if reused && attempt == 1 {
                                    continue 'attempt; // a dead reused connection: once more, fresh
                                }
                                return Err(FetchError::Url("EOF".to_string()));
                            }
                            return Err(FetchError::Url(format!("{BROKEN}unexpected EOF")));
                        }
                        Ok(n) => buf.extend_from_slice(&tmp[..n]),
                        Err(e) => {
                            if buf.is_empty() && reused && attempt == 1 {
                                continue 'attempt;
                            }
                            let t = conn.net_err("read", &e);
                            // nothing read yet: the peek error, undecorated; else a broken connection
                            return Err(FetchError::Url(if buf.is_empty() {
                                t
                            } else {
                                format!("{BROKEN}{t}")
                            }));
                        }
                    }
                };
                let parsed = parse_head(&buf[..head])?;
                let rest = buf.split_off(head);
                if (100..200).contains(&parsed.status) && parsed.status != 101 {
                    buf = rest;
                    scanned = 0;
                    continue;
                }
                break (parsed, rest);
            };
            status_seen.store(parsed.status + 1, Ordering::Relaxed);
            let framed = parsed.chunked || parsed.length.is_some();
            let (body, complete) = read_body(&mut conn, &mut rest, &parsed, limit).await?;
            if complete && framed && parsed.keep_alive && rest.is_empty() {
                *lock(&self.idle) = Some(Idle {
                    conn,
                    since: Instant::now(),
                });
            }
            return Ok(Fetched {
                status: parsed.status,
                body,
            });
        }
    }
}

/// The end of the head: the first empty line, `\r\n\r\n`, or the bare-LF forms Go's `textproto`
/// also takes (`\n\n`, `\n\r\n`). `from` is how much of `buf` an earlier call has searched.
fn find_head_end_from(buf: &[u8], from: usize) -> Option<usize> {
    let mut i = from.saturating_sub(2);
    #[cfg(test)]
    scan_count::add(buf.len().saturating_sub(i));
    while i < buf.len() {
        if buf[i] == b'\n' {
            match (buf.get(i + 1), buf.get(i + 2)) {
                (Some(b'\n'), _) => return Some(i + 2),
                (Some(b'\r'), Some(b'\n')) => return Some(i + 3),
                _ => {}
            }
        }
        i += 1;
    }
    None
}

/// A unit-test counter of the bytes `find_head_end_from` looked at and of its calls, per thread (R2-105:
/// the test of R1-30 asserts on the work the CALLER makes the search do, not on the clock).
#[cfg(test)]
mod scan_count {
    use std::cell::Cell;
    thread_local! { static C: Cell<(usize, usize)> = const { Cell::new((0, 0)) }; }
    pub fn add(bytes: usize) {
        C.with(|c| c.set((c.get().0 + bytes, c.get().1 + 1)));
    }
    pub fn take() -> (usize, usize) {
        C.with(|c| c.replace((0, 0)))
    }
}

/// [`find_head_end_from`] from the start: the one head search of every HTTP/1.1 reader in the crate.
pub fn find_head_end(buf: &[u8]) -> Option<usize> {
    find_head_end_from(buf, 0)
}

struct Head {
    status: u16,
    length: Option<u64>,
    chunked: bool,
    keep_alive: bool,
}

/// `http.ParseHTTPVersion`.
fn parse_http_version(v: &str) -> Option<(u8, u8)> {
    match v {
        "HTTP/1.1" => return Some((1, 1)),
        "HTTP/1.0" => return Some((1, 0)),
        _ => {}
    }
    let b = v.as_bytes();
    if !v.starts_with("HTTP/") || b.len() != 8 || b[6] != b'.' {
        return None;
    }
    if !b[5].is_ascii_digit() || !b[7].is_ascii_digit() {
        return None;
    }
    Some((b[5] - b'0', b[7] - b'0'))
}

fn parse_head(head: &[u8]) -> Result<Head, FetchError> {
    let text = String::from_utf8_lossy(head);
    let mut lines = text.split('\n').map(|l| l.strip_suffix('\r').unwrap_or(l));
    let status_line = lines.next().unwrap_or("");
    // `http.ReadResponse`: the line is cut at the first space, the status code (3 digits) is
    // checked before the version
    let Some((proto, status)) = status_line.split_once(' ') else {
        return Err(FetchError::Url(format!(
            "{BROKEN}malformed HTTP response {}",
            go_quote(status_line)
        )));
    };
    let status = status.trim_start_matches(' ');
    let code = status.split(' ').next().unwrap_or("");
    if code.len() != 3 || !code.bytes().all(|b| b.is_ascii_digit()) {
        return Err(FetchError::Url(format!(
            "{BROKEN}malformed HTTP status code {}",
            go_quote(code)
        )));
    }
    let Some((maj, min)) = parse_http_version(proto) else {
        return Err(FetchError::Url(format!(
            "{BROKEN}malformed HTTP version {}",
            go_quote(proto)
        )));
    };
    let status: u16 = code.parse().unwrap_or(0);
    let mut length = None;
    let mut cls: Vec<String> = Vec::new();
    let mut tes: Vec<String> = Vec::new();
    let mut conn_close = false;
    let mut conn_keep = false;
    for l in lines {
        if l.is_empty() {
            break;
        }
        let Some((k, v)) = l.split_once(':') else {
            continue;
        };
        let (k, v) = (k.trim().to_ascii_lowercase(), v.trim());
        match k.as_str() {
            "content-length" => {
                cls.push(v.to_string());
            }
            "transfer-encoding" => {
                tes.push(v.to_string());
            }
            "connection" => {
                for t in v.split(',') {
                    match t.trim().to_ascii_lowercase().as_str() {
                        "close" => conn_close = true,
                        "keep-alive" => conn_keep = true,
                        _ => {}
                    }
                }
            }
            _ => {}
        }
    }
    // as net/http (`parseTransferEncoding`, R2-73): ignored on HTTP/1.0; else one header whose
    // value is exactly `chunked` (any case), nothing else is supported
    let mut chunked = false;
    if (maj, min) >= (1, 1) && !tes.is_empty() {
        if tes.len() > 1 {
            let list: Vec<String> = tes.iter().map(|c| go_quote(c)).collect();
            return Err(FetchError::Url(format!(
                "{BROKEN}too many transfer encodings: [{}]",
                list.join(" ")
            )));
        }
        if !tes[0].eq_ignore_ascii_case("chunked") {
            return Err(FetchError::Url(format!(
                "{BROKEN}unsupported transfer encoding: {}",
                go_quote(&tes[0])
            )));
        }
        chunked = true;
    }
    // as net/http (fixLength): conflicting duplicates are always an error; a value that is not
    // a plain non-negative integer of at most 63 bits is one unless the body is chunked
    if cls.len() > 1 && cls[1..].iter().any(|c| c != &cls[0]) {
        let list: Vec<String> = cls.iter().map(|c| go_quote(c)).collect();
        return Err(FetchError::Url(format!(
            "{BROKEN}http: message cannot contain multiple Content-Length headers; got [{}]",
            list.join(" ")
        )));
    }
    if !chunked {
        if let Some(c) = cls.first() {
            match c.parse::<u64>() {
                Ok(n) if c.bytes().all(|b| b.is_ascii_digit()) && n <= i64::MAX as u64 => {
                    length = Some(n)
                }
                _ if c.is_empty() => {}
                _ => {
                    return Err(FetchError::Url(format!(
                        "{BROKEN}bad Content-Length {}",
                        go_quote(c)
                    )))
                }
            }
        }
    }
    if chunked {
        length = None;
    }
    if status == 204 || status == 304 || (100..200).contains(&status) {
        length = Some(0);
        chunked = false;
    }
    let keep_alive = !conn_close && ((maj, min) >= (1, 1) || conn_keep);
    Ok(Head {
        status,
        length,
        chunked,
        keep_alive,
    })
}

/// `parseHexUint` of `net/http/internal`, with its error texts.
fn parse_chunk_size(v: &str) -> Result<usize, &'static str> {
    if v.is_empty() {
        return Err("empty hex number for chunk length");
    }
    let mut n: u64 = 0;
    for (i, b) in v.bytes().enumerate() {
        let d = match b {
            b'0'..=b'9' => b - b'0',
            b'a'..=b'f' => b - b'a' + 10,
            b'A'..=b'F' => b - b'A' + 10,
            _ => return Err("invalid byte in chunk length"),
        };
        if i == 16 {
            return Err("http chunk length too large");
        }
        n = (n << 4) | u64::from(d);
    }
    Ok(usize::try_from(n).unwrap_or(usize::MAX))
}

/// Reads the body up to `limit` bytes. `complete` is whether the whole framed body was consumed
/// (so the connection may be reused).
async fn read_body(
    conn: &mut Conn,
    rest: &mut Vec<u8>,
    h: &Head,
    limit: usize,
) -> Result<(Vec<u8>, bool), FetchError> {
    let mut body: Vec<u8> = Vec::new();
    let mut tmp = [0u8; 8192];
    if h.chunked {
        loop {
            let line = read_line(conn, rest).await?;
            // `beginChunk`: trailing blanks, then the extension, then the hex number as it is
            let size_txt = line.trim_end_matches([' ', '\t']);
            let size_txt = size_txt.split(';').next().unwrap_or("");
            let size = parse_chunk_size(size_txt).map_err(|t| FetchError::Body(t.to_string()))?;
            if size == 0 {
                // trailers until an empty line
                loop {
                    if read_line(conn, rest).await?.is_empty() {
                        return Ok((body, true));
                    }
                }
            }
            let mut left = size;
            while left > 0 {
                if rest.is_empty() {
                    fill(conn, rest, &mut tmp).await?;
                }
                let take = left.min(rest.len());
                let room = limit.saturating_sub(body.len());
                body.extend(rest.drain(..take).take(room));
                left -= take;
                if body.len() >= limit {
                    return Ok((body, false));
                }
            }
            let crlf = read_line(conn, rest).await?;
            if !crlf.is_empty() {
                return Err(FetchError::Body("malformed chunked encoding".to_string()));
            }
        }
    }
    if let Some(total) = h.length {
        let mut left = total;
        while left > 0 {
            if rest.is_empty() {
                fill(conn, rest, &mut tmp).await?;
            }
            // a length over the address space is "everything that is here" (a 32-bit target
            // must not truncate it to 0 and spin)
            let take = usize::try_from(left).unwrap_or(usize::MAX).min(rest.len());
            let room = limit.saturating_sub(body.len());
            body.extend(rest.drain(..take).take(room));
            left -= take as u64;
            if body.len() >= limit {
                return Ok((body, left == 0));
            }
        }
        return Ok((body, true));
    }
    // no framing: the body runs to the end of the connection
    body.extend(rest.drain(..).take(limit));
    while body.len() < limit {
        let n = conn
            .read(&mut tmp)
            .await
            .map_err(|e| FetchError::Body(conn.net_err("read", &e)))?;
        if n == 0 {
            return Ok((body, false));
        }
        let room = limit - body.len();
        body.extend(tmp[..n].iter().copied().take(room));
    }
    Ok((body, false))
}

/// One read into `rest`; an EOF here is an unexpected one (the framing promised more).
async fn fill(conn: &mut Conn, rest: &mut Vec<u8>, tmp: &mut [u8]) -> Result<(), FetchError> {
    let n = conn
        .read(tmp)
        .await
        .map_err(|e| FetchError::Body(conn.net_err("read", &e)))?;
    if n == 0 {
        return Err(FetchError::Body("unexpected EOF".to_string()));
    }
    rest.extend_from_slice(&tmp[..n]);
    Ok(())
}

async fn read_line(conn: &mut Conn, rest: &mut Vec<u8>) -> Result<String, FetchError> {
    let mut tmp = [0u8; 4096];
    loop {
        if let Some(i) = rest.windows(2).position(|w| w == b"\r\n") {
            if i >= MAX_CHUNK_LINE {
                return Err(FetchError::Body("header line too long".to_string()));
            }
            let line: Vec<u8> = rest.drain(..i + 2).take(i).collect();
            return Ok(String::from_utf8_lossy(&line).into_owned());
        }
        if rest.len() >= MAX_CHUNK_LINE {
            return Err(FetchError::Body("header line too long".to_string()));
        }
        fill(conn, rest, &mut tmp).await?;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use tokio::net::TcpListener;

    /// A server that answers each connection's requests with `responses[n]` (n counting
    /// connections), each response given as bytes; `close_after` closes the connection after
    /// the first response.
    async fn server(responses: Vec<(Vec<u8>, bool)>) -> (String, Arc<AtomicUsize>) {
        let ln = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = ln.local_addr().unwrap().to_string();
        let conns = Arc::new(AtomicUsize::new(0));
        let c2 = conns.clone();
        tokio::spawn(async move {
            loop {
                let Ok((mut s, _)) = ln.accept().await else {
                    return;
                };
                let n = c2.fetch_add(1, Ordering::SeqCst);
                let (resp, close_after) = responses[n.min(responses.len() - 1)].clone();
                tokio::spawn(async move {
                    let mut buf = vec![0u8; 4096];
                    loop {
                        let mut got = Vec::new();
                        while find_head_end(&got).is_none() {
                            match s.read(&mut buf).await {
                                Ok(0) | Err(_) => return,
                                Ok(k) => got.extend_from_slice(&buf[..k]),
                            }
                        }
                        if s.write_all(&resp).await.is_err() || close_after {
                            return;
                        }
                    }
                });
            }
        });
        (addr, conns)
    }

    fn client(addr: &str) -> HttpClient {
        HttpClient::new(addr.to_string(), addr.to_string(), "/health".to_string())
    }

    const T: Duration = Duration::from_secs(2);

    #[tokio::test]
    async fn a_framed_response_keeps_the_connection_and_a_closed_one_is_not_reused() {
        let ok = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}".to_vec();
        let (addr, conns) = server(vec![(ok.clone(), false)]).await;
        let c = client(&addr);
        for _ in 0..3 {
            let f = c.get(T, 100).await.unwrap();
            assert_eq!((f.status, f.body.as_slice()), (200, &b"{}"[..]));
        }
        assert_eq!(conns.load(Ordering::SeqCst), 1, "keep-alive was not used");
        // the server closes after each response: the probe finds the idle connection dead and
        // dials again (the retry itself is the next test)
        let (addr, conns) = server(vec![(ok, true)]).await;
        let c = client(&addr);
        for _ in 0..3 {
            assert_eq!(c.get(T, 100).await.unwrap().status, 200);
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        assert_eq!(conns.load(Ordering::SeqCst), 3);
    }

    // R2-95: a kept connection that looks alive (the probe sees no data) but is closed by the
    // server on the next request WITHOUT a reply: the request is sent once more on a fresh
    // connection (net/http `shouldRetryRequest`), and only once.
    #[tokio::test]
    async fn a_kept_connection_that_dies_on_the_next_request_is_retried_once() {
        let ln = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = ln.local_addr().unwrap().to_string();
        let conns = Arc::new(AtomicUsize::new(0));
        let c2 = conns.clone();
        tokio::spawn(async move {
            let ok = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}";
            loop {
                let Ok((mut s, _)) = ln.accept().await else {
                    return;
                };
                let n = c2.fetch_add(1, Ordering::SeqCst);
                tokio::spawn(async move {
                    let mut buf = vec![0u8; 4096];
                    for round in 0.. {
                        // connection 0 answers its first request, reads the second and hangs up
                        // without a reply; later connections answer everything
                        if s.read(&mut buf).await.unwrap_or(0) == 0 {
                            return;
                        }
                        if n == 0 && round == 1 {
                            return;
                        }
                        if s.write_all(ok).await.is_err() {
                            return;
                        }
                    }
                });
            }
        });
        let c = client(&addr);
        assert_eq!(c.get(T, 100).await.unwrap().status, 200);
        assert_eq!(c.get(T, 100).await.unwrap().status, 200);
        assert_eq!(
            conns.load(Ordering::SeqCst),
            2,
            "one retry on a fresh connection"
        );
    }

    #[tokio::test]
    async fn chunked_close_delimited_and_limited_bodies() {
        let chunked = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n2;ext=1\r\nde\r\n0\r\nTrailer: x\r\n\r\n".to_vec();
        let (addr, conns) = server(vec![(chunked, false)]).await;
        let c = client(&addr);
        for _ in 0..2 {
            let f = c.get(T, 100).await.unwrap();
            assert_eq!(f.body, b"abcde");
        }
        assert_eq!(
            conns.load(Ordering::SeqCst),
            1,
            "a fully read chunked body keeps the connection"
        );

        let until_close = b"HTTP/1.0 200 OK\r\n\r\nhello".to_vec();
        let (addr, conns) = server(vec![(until_close, true)]).await;
        let c = client(&addr);
        assert_eq!(c.get(T, 100).await.unwrap().body, b"hello");
        assert_eq!(c.get(T, 100).await.unwrap().body, b"hello");
        assert_eq!(conns.load(Ordering::SeqCst), 2);

        let mut big = b"HTTP/1.1 200 OK\r\nContent-Length: 5000\r\n\r\n".to_vec();
        big.extend(std::iter::repeat_n(b'x', 5000));
        let (addr, conns) = server(vec![(big, false)]).await;
        let c = client(&addr);
        let f = c.get(T, 11).await.unwrap();
        assert_eq!(f.body.len(), 11, "the body is capped at the limit");
        c.get(T, 11).await.unwrap();
        assert_eq!(
            conns.load(Ordering::SeqCst),
            2,
            "a partly read body must not be reused"
        );
    }

    #[tokio::test]
    async fn redirects_are_statuses_and_garbage_is_malformed() {
        let r = b"HTTP/1.1 302 Found\r\nLocation: /call\r\nContent-Length: 0\r\n\r\n".to_vec();
        let (addr, _) = server(vec![(r, false)]).await;
        let f = client(&addr).get(T, 100).await.unwrap();
        assert_eq!(f.status, 302, "a redirect must not be followed");
        let (addr, _) = server(vec![(b"garbage\r\n\r\n".to_vec(), true)]).await;
        let e = client(&addr).get(T, 100).await.unwrap_err();
        assert_eq!(
            e,
            FetchError::Url(
                "net/http: HTTP/1.x transport connection broken: malformed HTTP response \"garbage\"".to_string()
            )
        );
        let (addr, _) = server(vec![(
            b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n12345".to_vec(),
            true,
        )])
        .await;
        let e = client(&addr).get(T, 100).await.unwrap_err();
        assert_eq!(e, FetchError::Body("unexpected EOF".to_string()));
        let (addr, _) = server(vec![(
            b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx".to_vec(),
            false,
        )])
        .await;
        let f = client(&addr).get(T, 100).await.unwrap();
        assert_eq!((f.status, f.body.as_slice()), (200, &b"x"[..]));
    }

    #[tokio::test]
    async fn timeouts_say_context_deadline_exceeded_and_refusals_name_the_dial() {
        let ln = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = ln.local_addr().unwrap().to_string();
        // accepts and never answers
        let _hold = tokio::spawn(async move {
            let mut held = Vec::new();
            while let Ok((s, _)) = ln.accept().await {
                held.push(s);
            }
        });
        let e = client(&addr)
            .get(Duration::from_millis(100), 100)
            .await
            .unwrap_err();
        assert_eq!(e, FetchError::Url("context deadline exceeded".to_string()));
        let dead = {
            let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
            l.local_addr().unwrap().to_string()
        };
        let e = client(&dead).get(T, 100).await.unwrap_err();
        assert_eq!(
            e,
            FetchError::Url(format!("dial tcp {dead}: connect: connection refused"))
        );
    }

    // R2-6: the text of a dial that timed out depends on whether the REQUEST deadline is the
    // one that expired (Go: the context error wins) or only a per-address share of it.
    #[tokio::test(start_paused = true)]
    async fn a_dial_timeout_reads_like_gos_context_or_dial_error() {
        let a: SocketAddr = "192.168.2.1:1".parse().unwrap();
        let t0 = Instant::now();
        let deadline = t0 + Duration::from_secs(3);
        let timed_out = std::io::Error::from(std::io::ErrorKind::TimedOut);
        assert_eq!(
            dial_failure(a, &timed_out, deadline, deadline),
            "context deadline exceeded"
        );
        assert_eq!(
            dial_failure(a, &timed_out, deadline + Duration::from_millis(1), deadline),
            "context deadline exceeded"
        );
        assert_eq!(
            dial_failure(a, &timed_out, t0 + Duration::from_secs(1), deadline),
            "dial tcp 192.168.2.1:1: i/o timeout"
        );
        let refused = std::io::Error::from(std::io::ErrorKind::ConnectionRefused);
        assert_eq!(
            dial_failure(a, &refused, deadline, deadline),
            "dial tcp 192.168.2.1:1: connect: connection refused"
        );
    }

    // R3B-3: the caller of the helper. A connect that never completes, run through the client's own
    // dial path, reads `context deadline exceeded` (the request deadline expired); with two
    // addresses, the first black-holed and the second refused, the request deadline is not yet
    // reached when the first one's share runs out, and the error is the FIRST address's
    // `dial tcp a: i/o timeout`. Mutation: `connect_addrs` wording every error as `connect: ..`.
    #[tokio::test(start_paused = true)]
    async fn the_client_words_a_timed_out_dial_through_dial_failure() {
        let c = HttpClient::new("x:80".into(), "x".into(), "/".into());
        let a1: SocketAddr = "192.168.2.1:80".parse().unwrap();
        let a2: SocketAddr = "192.168.2.2:80".parse().unwrap();
        let hole = |_: SocketAddr| std::future::pending::<std::io::Result<tokio::net::TcpStream>>();
        let r = c
            .connect_addrs(&[a1], Instant::now() + Duration::from_secs(3), hole)
            .await;
        assert_eq!(
            r.err(),
            Some(FetchError::Url("context deadline exceeded".into()))
        );
        let first_black_holed = |a: SocketAddr| async move {
            if a == a1 {
                std::future::pending::<std::io::Result<tokio::net::TcpStream>>().await
            } else {
                Err(std::io::Error::from(std::io::ErrorKind::ConnectionRefused))
            }
        };
        let r = c
            .connect_addrs(
                &[a1, a2],
                Instant::now() + Duration::from_secs(3),
                first_black_holed,
            )
            .await;
        assert_eq!(
            r.err(),
            Some(FetchError::Url("dial tcp 192.168.2.1:80: i/o timeout".into()))
        );
    }

    // R3A-12: a head whose code is `000` is a head: a body cut off by the deadline after it is a
    // `Body` error, as for any other status (0 used to mean "no head was read").
    #[tokio::test]
    async fn a_head_with_code_000_is_still_a_head_when_the_body_is_cut_off() {
        let ln = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = ln.local_addr().unwrap().to_string();
        tokio::spawn(async move {
            let (mut s, _) = ln.accept().await.unwrap();
            let mut buf = [0u8; 1024];
            let _ = s.read(&mut buf).await;
            let _ = s
                .write_all(b"HTTP/1.1 000 X\r\nContent-Length: 9\r\n\r\nab")
                .await;
            tokio::time::sleep(Duration::from_secs(5)).await;
        });
        let (status, r) = client(&addr)
            .get_status(Duration::from_millis(300), 100)
            .await;
        assert_eq!(status, 0);
        assert_eq!(
            r.unwrap_err(),
            FetchError::Body("context deadline exceeded".into())
        );
    }

    // R2-73: net/http takes `Transfer-Encoding` only as the single token `chunked`, and ignores
    // it on HTTP/1.0
    #[tokio::test]
    async fn only_the_single_token_chunked_is_a_transfer_encoding() {
        let broken = "net/http: HTTP/1.x transport connection broken: ";
        for (te, want) in [
            (
                "gzip, chunked",
                "unsupported transfer encoding: \"gzip, chunked\"",
            ),
            ("xchunkedx", "unsupported transfer encoding: \"xchunkedx\""),
            ("identity", "unsupported transfer encoding: \"identity\""),
        ] {
            let resp = format!("HTTP/1.1 200 OK\r\nTransfer-Encoding: {te}\r\n\r\n0\r\n\r\n");
            let (addr, _) = server(vec![(resp.into_bytes(), true)]).await;
            let e = client(&addr).get(T, 100).await.unwrap_err();
            assert_eq!(e, FetchError::Url(format!("{broken}{want}")), "{te}");
        }
        let two = "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n";
        let (addr, _) = server(vec![(two.as_bytes().to_vec(), true)]).await;
        let e = client(&addr).get(T, 100).await.unwrap_err();
        assert_eq!(
            e,
            FetchError::Url(format!(
                "{broken}too many transfer encodings: [\"chunked\" \"chunked\"]"
            ))
        );
        // any case is chunked; on HTTP/1.0 the header is ignored (the body runs to the close)
        let (addr, _) = server(vec![(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: CHUNKED\r\n\r\n2\r\nhi\r\n0\r\n\r\n".to_vec(),
            true,
        )])
        .await;
        assert_eq!(client(&addr).get(T, 100).await.unwrap().body, b"hi");
        let (addr, _) = server(vec![(
            b"HTTP/1.0 200 OK\r\nTransfer-Encoding: gzip, chunked\r\n\r\nhello".to_vec(),
            true,
        )])
        .await;
        assert_eq!(client(&addr).get(T, 100).await.unwrap().body, b"hello");
    }

    #[tokio::test]
    async fn lax_framing_is_refused_as_net_http_refuses_it() {
        let broken = "net/http: HTTP/1.x transport connection broken: ";
        for (resp, want) in [
            ("HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n{}", format!("{broken}bad Content-Length \"-1\"")),
            ("HTTP/1.1 200 OK\r\nContent-Length: +2\r\n\r\n{}", format!("{broken}bad Content-Length \"+2\"")),
            ("HTTP/1.1 200 OK\r\nContent-Length: 2x\r\n\r\n{}", format!("{broken}bad Content-Length \"2x\"")),
            (
                "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n{}",
                format!("{broken}http: message cannot contain multiple Content-Length headers; got [\"2\" \"3\"]"),
            ),
        ] {
            let (addr, _) = server(vec![(resp.as_bytes().to_vec(), true)]).await;
            let e = client(&addr).get(T, 100).await.unwrap_err();
            assert_eq!(e, FetchError::Url(want), "{resp:?}");
        }
        // equal duplicates are fine, as is a bad length next to a chunked body
        let (addr, _) = server(vec![(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}".to_vec(),
            true,
        )])
        .await;
        assert_eq!(client(&addr).get(T, 100).await.unwrap().body, b"{}");
        let (addr, _) = server(vec![(b"HTTP/1.1 200 OK\r\nContent-Length: -1\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n".to_vec(), true)]).await;
        assert_eq!(client(&addr).get(T, 100).await.unwrap().body, b"{}");
        // a chunk size with a sign
        let (addr, _) = server(vec![(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n+2\r\n{}\r\n0\r\n\r\n".to_vec(),
            true,
        )])
        .await;
        let e = client(&addr).get(T, 100).await.unwrap_err();
        assert_eq!(
            e,
            FetchError::Body("invalid byte in chunk length".to_string())
        );
    }

    // R1-9: an https URL is polled over TLS: verified against the roots it was given, the
    // connection kept for the next poll, a certificate it does not trust named as Go names it.
    fn tls_server() -> (
        tokio_rustls::TlsAcceptor,
        rustls::pki_types::CertificateDer<'static>,
    ) {
        let ck = rcgen::generate_simple_self_signed(vec!["127.0.0.1".to_string()]).unwrap();
        let cert = ck.cert.der().clone();
        let key = rustls::pki_types::PrivatePkcs8KeyDer::from(ck.signing_key.serialize_der());
        let provider = Arc::new(rustls::crypto::aws_lc_rs::default_provider());
        let cfg = rustls::ServerConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_no_client_auth()
            .with_single_cert(vec![cert.clone()], key.into())
            .unwrap();
        (tokio_rustls::TlsAcceptor::from(Arc::new(cfg)), cert)
    }

    #[tokio::test]
    async fn https_is_polled_over_tls_and_an_untrusted_certificate_is_named() {
        let (acceptor, cert) = tls_server();
        let ln = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = ln.local_addr().unwrap().to_string();
        let conns = Arc::new(AtomicUsize::new(0));
        let c2 = conns.clone();
        tokio::spawn(async move {
            while let Ok((s, _)) = ln.accept().await {
                c2.fetch_add(1, Ordering::SeqCst);
                let acceptor = acceptor.clone();
                tokio::spawn(async move {
                    let Ok(mut s) = acceptor.accept(s).await else {
                        return;
                    };
                    let mut buf = vec![0u8; 4096];
                    loop {
                        let mut got = Vec::new();
                        while find_head_end(&got).is_none() {
                            match s.read(&mut buf).await {
                                Ok(0) | Err(_) => return,
                                Ok(k) => got.extend_from_slice(&buf[..k]),
                            }
                        }
                        let ok = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}";
                        if s.write_all(ok).await.is_err() {
                            return;
                        }
                    }
                });
            }
        });
        let mut roots = rustls::RootCertStore::empty();
        roots.add(cert).unwrap();
        let c = client(&addr).with_tls(Arc::new(Tls::with_roots(roots)), "127.0.0.1".into());
        for _ in 0..3 {
            let f = c.get(T, 100).await.unwrap();
            assert_eq!((f.status, f.body.as_slice()), (200, &b"{}"[..]));
        }
        assert_eq!(
            conns.load(Ordering::SeqCst),
            1,
            "the TLS connection was not reused"
        );
        let c = client(&addr).with_tls(
            Arc::new(Tls::with_roots(rustls::RootCertStore::empty())),
            "127.0.0.1".into(),
        );
        assert_eq!(
            c.get(T, 100).await.unwrap_err(),
            FetchError::Url(
                "tls: failed to verify certificate: x509: certificate signed by unknown authority"
                    .to_string()
            )
        );
        // plain HTTP to a TLS port is a handshake failure, not a hang
        let c = client(&addr);
        assert!(c.get(T, 100).await.is_err());
    }

    // R1-30: the head search resumes where the last read stopped; a header that arrives in many
    // reads must not cost a rescan of everything before it per read.
    #[test]
    fn the_head_search_resumes_where_the_last_read_stopped() {
        // a terminator that straddles two reads is still found
        let mut buf = b"HTTP/1.1 200 OK\r\nA: b\r\n\r".to_vec();
        let scanned = buf.len();
        assert_eq!(find_head_end_from(&buf, 0), None);
        buf.push(b'\n');
        assert_eq!(find_head_end_from(&buf, scanned), Some(buf.len()));
    }

    // R2-105: the CALLER of the search (`get`) hands it the offset it keeps. A 64 KiB head is read
    // in 4 KiB pieces; the bytes searched in all must stay near the head's length. A caller that
    // searches from 0 each time reads about 530 KB for it.
    #[tokio::test]
    async fn the_head_search_does_not_rescan_what_it_has_seen() {
        let mut head = b"HTTP/1.1 200 OK\r\n".to_vec();
        for _ in 0..16 {
            head.extend_from_slice(b"X-Pad: ");
            head.extend(std::iter::repeat_n(b'a', 4_000));
            head.extend_from_slice(b"\r\n");
        }
        head.extend_from_slice(b"Content-Length: 2\r\n\r\n{}");
        let (addr, _) = server(vec![(head.clone(), true)]).await;
        let c = client(&addr);
        let _ = scan_count::take();
        let f = c.get(Duration::from_secs(10), 100).await.unwrap();
        assert_eq!(f.body, b"{}");
        let (bytes, calls) = scan_count::take();
        assert!(
            calls >= 8,
            "the head arrived in {calls} pieces only: the test proves little"
        );
        // the server's own search of the small request is in the count too (about 100 bytes)
        assert!(
            bytes <= head.len() + 2 * calls + 1024,
            "{bytes} bytes searched in {calls} calls for a head of {}",
            head.len()
        );
    }

    // R1-90: textproto takes a bare LF as a line end, in the head too.
    #[tokio::test]
    async fn a_head_with_bare_line_feeds_is_read() {
        for resp in [
            "HTTP/1.1 200 OK\nContent-Length: 2\n\n{}",
            "HTTP/1.1 200 OK\nContent-Length: 2\n\r\n{}",
            "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\n{}",
        ] {
            let (addr, _) = server(vec![(resp.as_bytes().to_vec(), true)]).await;
            let f = client(&addr).get(T, 100).await.unwrap();
            assert_eq!((f.status, f.body.as_slice()), (200, &b"{}"[..]), "{resp:?}");
        }
    }

    // R1-89 (c) to (d): the texts of net/http's ReadResponse and chunked reader (read from the Go
    // source; the status code is checked before the version, a status line may have several spaces).
    #[tokio::test]
    async fn status_line_and_chunk_errors_read_like_net_http() {
        let broken = "net/http: HTTP/1.x transport connection broken: ";
        for (resp, want) in [
            (
                "garbage 200 OK\r\n\r\n",
                format!("{broken}malformed HTTP version \"garbage\""),
            ),
            (
                "HTTP/1.1 20 OK\r\n\r\n",
                format!("{broken}malformed HTTP status code \"20\""),
            ),
            (
                "HTTP/1.1\r\n\r\n",
                format!("{broken}malformed HTTP response \"HTTP/1.1\""),
            ),
            (
                "HTTP/12.1 200 OK\r\n\r\n",
                format!("{broken}malformed HTTP version \"HTTP/12.1\""),
            ),
        ] {
            let (addr, _) = server(vec![(resp.as_bytes().to_vec(), true)]).await;
            let e = client(&addr).get(T, 100).await.unwrap_err();
            assert_eq!(e, FetchError::Url(want), "{resp:?}");
        }
        // two spaces before the code are accepted (`TrimLeft`)
        let (addr, _) = server(vec![(
            b"HTTP/1.1  200 OK\r\nContent-Length: 2\r\n\r\n{}".to_vec(),
            true,
        )])
        .await;
        assert_eq!(client(&addr).get(T, 100).await.unwrap().status, 200);
        let long = format!("{};x\r\n", "1".repeat(5000));
        for (chunk, want) in [
            ("\r\n".to_string(), "empty hex number for chunk length"),
            (
                "11111111111111111\r\n".to_string(),
                "http chunk length too large",
            ),
            ("zz\r\n".to_string(), "invalid byte in chunk length"),
            (long, "header line too long"),
        ] {
            let resp = format!("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n{chunk}");
            let (addr, _) = server(vec![(resp.into_bytes(), true)]).await;
            let e = client(&addr).get(T, 100).await.unwrap_err();
            assert_eq!(e, FetchError::Body(want.to_string()), "{chunk:?}");
        }
    }

    // R1-89 (a): a reset while the BODY is read is a net.OpError, like the one on the header path.
    #[tokio::test]
    async fn a_reset_in_the_body_is_worded_like_a_net_op_error() {
        let ln = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = ln.local_addr().unwrap().to_string();
        tokio::spawn(async move {
            let (mut s, _) = ln.accept().await.unwrap();
            let mut b = [0u8; 1024];
            let _ = s.read(&mut b).await;
            s.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n12345")
                .await
                .unwrap();
            #[allow(deprecated)]
            s.set_linger(Some(Duration::ZERO)).unwrap();
            drop(s); // RST
        });
        let FetchError::Body(t) = client(&addr).get(T, 100).await.unwrap_err() else {
            panic!("not a body error");
        };
        assert!(
            t.starts_with("read tcp 127.0.0.1:") && t.ends_with(": read: connection reset by peer"),
            "{t}"
        );
    }

    // R1-89 (b): Go's transport allows 10 MiB of response header; the message names that number.
    #[tokio::test]
    async fn the_header_limit_is_gos_ten_mib() {
        let mut big = b"HTTP/1.1 200 OK\r\n".to_vec();
        for _ in 0..40 {
            big.extend_from_slice(b"X-Pad: ");
            big.extend(std::iter::repeat_n(b'a', 50_000));
            big.extend_from_slice(b"\r\n");
        }
        big.extend_from_slice(b"Content-Length: 2\r\n\r\n{}");
        let (addr, _) = server(vec![(big, true)]).await;
        let f = client(&addr)
            .get(Duration::from_secs(10), 100)
            .await
            .unwrap();
        assert_eq!(f.body, b"{}", "2 MB of header is within the limit");
        let mut huge = b"HTTP/1.1 200 OK\r\n".to_vec();
        for _ in 0..(10 << 20) / 50_000 + 2 {
            huge.extend_from_slice(b"X-Pad: ");
            huge.extend(std::iter::repeat_n(b'a', 50_000));
            huge.extend_from_slice(b"\r\n");
        }
        let (addr, _) = server(vec![(huge, true)]).await;
        let e = client(&addr)
            .get(Duration::from_secs(10), 100)
            .await
            .unwrap_err();
        assert_eq!(
            e,
            FetchError::Url(format!(
                "{BROKEN}net/http: server response headers exceeded 10485760 bytes; aborted"
            ))
        );
    }

    // R1-115: a lookup that yields nothing and one that fails say the same, host only.
    #[test]
    fn the_no_such_host_text_names_the_host_only() {
        assert_eq!(
            no_such_host("gate:8091"),
            FetchError::Url("dial tcp: lookup gate: no such host".to_string())
        );
    }
}
