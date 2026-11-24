//! `ws.go`: the minimal RFC 6455 client the Protect events stream needs and nothing more: one GET
//! upgrade with the `X-API-KEY` header, text messages in (with fragmentation), ping/pong, a close
//! frame, masked frames out.
//!
//! The Go reader is a blocking loop over a `bufio.Reader`; here the frame parser is a pure
//! incremental state machine ([`Decoder`]: bytes in, events out, no I/O) so it can be driven
//! byte by byte and fuzzed, and the connection pump ([`pump`]) is one task that selects over the
//! socket, the ping interval, the read deadline and the cancellation token. Nothing in the
//! decoder trusts the peer: a frame is refused as soon as its header says it is too big, before a
//! payload byte is buffered, and what is held is at most one frame plus one read.
//!
//! Differences from Go (PARITY.md 108, 109, 111, 112): the response head is parsed by hand (a head over
//! 64 KiB is refused), and a peer that sends a frame header and then stalls is cut by the
//! pong-wait deadline exactly as in Go but without Go's up-front `make([]byte, n)` of the
//! announced length.

use std::time::Duration;

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::{timeout, timeout_at, Instant};

use crate::cancel::CancelToken;
use crate::gojson::io_err;
use crate::gostd::http1::find_head_end;
use crate::gostd::net::{join_host_port, split_host_port};
use crate::gostd::tls::{self, Tls};

pub const WS_GUID: &str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
/// `maxMessage`: a message (all fragments together) larger than this drops the connection.
pub const MAX_MESSAGE: usize = 1 << 20;
/// The write deadline of `writeFrame`.
pub const WRITE_TIMEOUT: Duration = Duration::from_secs(5);
/// A response head larger than this is refused (Go reads any size).
const MAX_HEAD: usize = 64 << 10;

/// Why a connection's read side ended. The texts are Go's (`errWSClosed`, `errWSProtocol`,
/// `errWSTooBig`, `io.EOF`, `io.ErrUnexpectedEOF`, the deadline error).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WsError {
    Closed,
    Protocol,
    TooBig,
    Eof,
    UnexpectedEof,
    Timeout,
    Io(String),
    Cancelled,
}

impl std::fmt::Display for WsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            WsError::Closed => f.write_str("websocket closed by the console"),
            WsError::Protocol => f.write_str("websocket protocol error"),
            WsError::TooBig => f.write_str("websocket message too large"),
            WsError::Eof => f.write_str("EOF"),
            WsError::UnexpectedEof => f.write_str("unexpected EOF"),
            WsError::Timeout => f.write_str("i/o timeout"),
            WsError::Io(s) => f.write_str(s),
            WsError::Cancelled => f.write_str("cancelled"),
        }
    }
}

/// One thing the decoder found.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    /// a complete TEXT message (fragments joined); binary messages are dropped inside
    Message(Vec<u8>),
    /// a ping: the caller answers with a pong carrying the same payload
    Ping(Vec<u8>),
    Pong,
    /// a close frame: the caller answers with an empty close frame
    Close,
}

/// The frame parser and message assembler of `readMessage`, without the I/O.
#[derive(Default)]
pub struct Decoder {
    buf: Vec<u8>,
    pos: usize,
    msg: Vec<u8>,
    started: bool,
    is_text: bool,
    frames: u64,
    /// consecutive empty continuation frames of the message being assembled
    empty_frags: u32,
}

/// RFC 6455 5.5: a control frame carries at most this many payload bytes and is never fragmented.
const MAX_CONTROL_PAYLOAD: u64 = 125;
/// Empty continuation frames tolerated in a row; each restarts the watcher's read deadline, so a
/// peer could otherwise hold it open with nothing (DEV-796 D9).
const MAX_EMPTY_FRAGMENTS: u32 = 64;

impl Decoder {
    pub fn new() -> Self {
        Self::default()
    }

    /// Appends received bytes. Call [`Decoder::next_event`] until it returns `Ok(None)` before the
    /// next push, which keeps `buf` to one frame plus one read.
    pub fn push(&mut self, data: &[u8]) {
        if self.pos > 0 && self.pos == self.buf.len() {
            self.buf.clear();
            self.pos = 0;
        } else if self.pos > 64 << 10 {
            self.buf.drain(..self.pos);
            self.pos = 0;
        }
        self.buf.extend_from_slice(data);
    }

    /// Frames completed so far (a read deadline restarts for each).
    pub fn frames(&self) -> u64 {
        self.frames
    }

    /// What a stream that ends now has cut: Go reads a frame field by field (the two header
    /// bytes, the extended length, the mask, the payload) with `io.ReadFull`, which says
    /// `io.EOF` when it read nothing of the field it was asked for and `io.ErrUnexpectedEOF`
    /// when it read part of it. So a stream that ends exactly between two fields (or two frames)
    /// is a plain EOF, and one that ends inside a field is an unexpected EOF.
    pub fn eof_error(&self) -> WsError {
        let b = &self.buf[self.pos..];
        let have = b.len();
        if have == 0 {
            return WsError::Eof;
        }
        let mut bounds = vec![0usize, 2];
        if have >= 2 {
            let ext = match b[1] & 0x7f {
                126 => 2,
                127 => 8,
                _ => 0,
            };
            bounds.push(2 + ext);
            if have >= 2 + ext {
                let mask = if b[1] & 0x80 != 0 { 4 } else { 0 };
                bounds.push(2 + ext + mask);
                if have >= 2 + ext + mask {
                    let n = match ext {
                        0 => (b[1] & 0x7f) as usize,
                        2 => u16::from_be_bytes([b[2], b[3]]) as usize,
                        _ => {
                            let mut x = [0u8; 8];
                            x.copy_from_slice(&b[2..10]);
                            u64::from_be_bytes(x).min(usize::MAX as u64 / 2) as usize
                        }
                    };
                    bounds.push(2 + ext + mask + n);
                }
            }
        }
        if bounds.contains(&have) {
            WsError::Eof
        } else {
            WsError::UnexpectedEof
        }
    }

    /// Bytes held (header and payload of the frame being read).
    pub fn buffered(&self) -> usize {
        self.buf.len() - self.pos
    }

    /// The next event, `Ok(None)` when more bytes are needed.
    pub fn next_event(&mut self) -> Result<Option<Event>, WsError> {
        loop {
            let b = &self.buf[self.pos..];
            if b.len() < 2 {
                return Ok(None);
            }
            let fin = b[0] & 0x80 != 0;
            let op = b[0] & 0x0f;
            if b[0] & 0x70 != 0 {
                return Err(WsError::Protocol);
            }
            let masked = b[1] & 0x80 != 0;
            let (n, mut hdr) = match b[1] & 0x7f {
                126 => {
                    if b.len() < 4 {
                        return Ok(None);
                    }
                    (u16::from_be_bytes([b[2], b[3]]) as u64, 4usize)
                }
                127 => {
                    if b.len() < 10 {
                        return Ok(None);
                    }
                    let mut x = [0u8; 8];
                    x.copy_from_slice(&b[2..10]);
                    (u64::from_be_bytes(x), 10usize)
                }
                s => (s as u64, 2usize),
            };
            if n > MAX_MESSAGE as u64 {
                return Err(WsError::TooBig);
            }
            if (0x8..=0xA).contains(&op) && (!fin || n > MAX_CONTROL_PAYLOAD) {
                return Err(WsError::Protocol);
            }
            let n = n as usize;
            let mut mask = [0u8; 4];
            if masked {
                if b.len() < hdr + 4 {
                    return Ok(None);
                }
                mask.copy_from_slice(&b[hdr..hdr + 4]);
                hdr += 4;
            }
            if b.len() < hdr + n {
                return Ok(None);
            }
            let mut p = b[hdr..hdr + n].to_vec();
            if masked {
                for (i, x) in p.iter_mut().enumerate() {
                    *x ^= mask[i % 4];
                }
            }
            self.pos += hdr + n;
            self.frames += 1;
            match op {
                0x9 => return Ok(Some(Event::Ping(p))),
                0xA => return Ok(Some(Event::Pong)),
                0x8 => return Ok(Some(Event::Close)),
                0x0..=0x2 => {
                    if op == 0x0 {
                        if !self.started {
                            return Err(WsError::Protocol); // a continuation with nothing to continue
                        }
                    } else {
                        if self.started {
                            return Err(WsError::Protocol); // a new message before the last one finished
                        }
                        self.started = true;
                        self.is_text = op == 0x1;
                        self.msg.clear();
                    }
                    if op == 0x0 && p.is_empty() {
                        self.empty_frags += 1;
                        if self.empty_frags > MAX_EMPTY_FRAGMENTS {
                            return Err(WsError::Protocol);
                        }
                    } else {
                        self.empty_frags = 0;
                    }
                    if self.msg.len() + p.len() > MAX_MESSAGE {
                        return Err(WsError::TooBig);
                    }
                    self.msg.append(&mut p);
                    if fin {
                        if self.is_text {
                            self.started = false;
                            return Ok(Some(Event::Message(std::mem::take(&mut self.msg))));
                        }
                        self.started = false; // a binary message: drop it
                        self.msg.clear();
                    }
                }
                _ => return Err(WsError::Protocol),
            }
        }
    }
}

/// `writeFrame`'s bytes: one final, masked frame.
pub fn encode_frame(op: u8, payload: &[u8], mask: [u8; 4]) -> Vec<u8> {
    let mut out = Vec::with_capacity(payload.len() + 14);
    out.push(0x80 | op);
    match payload.len() {
        n if n < 126 => out.push(0x80 | n as u8),
        n if n < 1 << 16 => {
            out.push(0x80 | 126);
            out.extend_from_slice(&(n as u16).to_be_bytes());
        }
        n => {
            out.push(0x80 | 127);
            out.extend_from_slice(&(n as u64).to_be_bytes());
        }
    }
    out.extend_from_slice(&mask);
    out.extend(payload.iter().enumerate().map(|(i, b)| b ^ mask[i % 4]));
    out
}

pub trait Io: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> Io for T {}

/// One established websocket.
pub struct WsConn {
    io: Box<dyn Io>,
    dec: Decoder,
}

/// A dial failure. [`DialError::Status`] carries the code only: the body of a refusal can hold
/// anything and is never kept.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DialError {
    Status(u16),
    /// `wsCertError`: the console's certificate is not trusted
    Cert,
    /// the caller's cancellation token fired
    Cancelled,
    /// any other failure; the text starts `connect:`, `tls:` or `upgrade:`
    Msg(String),
}

impl std::fmt::Display for DialError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            DialError::Status(c) => write!(f, "HTTP {c}"),
            DialError::Cert => f.write_str("certificate not trusted"),
            DialError::Cancelled => f.write_str("cancelled"),
            DialError::Msg(s) => f.write_str(s),
        }
    }
}

/// `reasonFor`: a dial or read error as a short, secret-free cause for `AvailabilityChanged`.
pub fn reason_for(err: &DialError) -> String {
    match err {
        DialError::Status(c) => format!("events stream refused (HTTP {c})"),
        DialError::Cert => "console certificate not trusted (verify_ssl)".to_string(),
        DialError::Msg(s) if s.starts_with("tls:") => "console TLS handshake failed".to_string(),
        _ => "console unreachable".to_string(),
    }
}

/// May `s` be written into a header line? A host: no byte <= 0x20 or 0x7f; a key: no control
/// byte (a space is allowed in a key).
pub fn header_safe(s: &str, is_key: bool) -> bool {
    !s.bytes()
        .any(|b| b < 0x20 || b == 0x7f || (!is_key && b == b' '))
}

/// `scrub`: an error text with the secret replaced.
pub fn scrub(s: &str, secret: &str) -> String {
    if secret.is_empty() {
        s.to_string()
    } else {
        s.replace(secret, "***")
    }
}

/// What `dialWS` needs.
pub struct DialParams<'a> {
    pub scheme: &'a str,
    pub host: &'a str,
    pub path: &'a str,
    pub api_key: &'a str,
    pub tls: &'a Tls,
    pub dial_timeout: Duration,
}

/// `Sec-WebSocket-Accept` for `key`.
pub fn accept_for(key: &str) -> String {
    let d = aws_lc_rs::digest::digest(
        &aws_lc_rs::digest::SHA1_FOR_LEGACY_USE_ONLY,
        format!("{key}{WS_GUID}").as_bytes(),
    );
    crate::util::base64_encode(d.as_ref())
}

/// The status code and the `Sec-WebSocket-Accept` value of a response head (`http.ReadResponse`
/// for the one thing `dialWS` reads).
fn parse_head(head: &[u8]) -> Option<(u16, String)> {
    let text = std::str::from_utf8(head).ok()?;
    let mut lines = text.split('\n').map(|l| l.strip_suffix('\r').unwrap_or(l));
    let first = lines.next()?;
    let (proto, rest) = first.split_once(' ')?;
    if !proto.starts_with("HTTP/") {
        return None;
    }
    let code = rest.split(' ').next()?;
    if code.len() != 3 || !code.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let code: u16 = code.parse().ok()?;
    let mut accept = String::new();
    let mut seen = false;
    for l in lines {
        if l.is_empty() {
            break;
        }
        if let Some((k, v)) = l.split_once(':') {
            if !seen && k.trim().eq_ignore_ascii_case("Sec-WebSocket-Accept") {
                accept = v.trim_matches(|c| c == ' ' || c == '\t').to_string();
                seen = true;
            }
        }
    }
    Some((code, accept))
}

/// `dialWS`: opens the websocket at scheme://host+path (scheme "https" means TLS, anything else
/// plain TCP; the Protect console is always https, plain exists for the loopback fake in tests).
/// Errors never include the API key.
pub async fn dial(cancel: &CancelToken, p: &DialParams<'_>) -> Result<WsConn, DialError> {
    tokio::select! {
        _ = cancel.cancelled() => Err(DialError::Cancelled),
        r = dial_inner(p) => r,
    }
}

async fn dial_inner(p: &DialParams<'_>) -> Result<WsConn, DialError> {
    // the host and the key go into header lines: refuse anything that could end one
    if !header_safe(p.host, false) || !header_safe(p.api_key, true) {
        return Err(DialError::Msg(
            "upgrade: refusing a host or key with control characters".into(),
        ));
    }
    let split = split_host_port(p.host).ok();
    let addr = match &split {
        Some(_) => p.host.to_string(),
        None => join_host_port(p.host, if p.scheme == "https" { "443" } else { "80" }),
    };
    let tcp = match timeout(p.dial_timeout, TcpStream::connect(addr.as_str())).await {
        Ok(Ok(s)) => s,
        Ok(Err(e)) => {
            return Err(DialError::Msg(format!(
                "connect: {}",
                scrub(&io_err(&e), p.api_key)
            )))
        }
        Err(_) => return Err(DialError::Msg("connect: i/o timeout".into())),
    };
    let _ = tcp.set_nodelay(true);
    // One deadline covers the handshake and the upgrade, as `raw.SetDeadline` does.
    let deadline = Instant::now() + p.dial_timeout;
    let mut io: Box<dyn Io> = if p.scheme == "https" {
        let hostname = split.map_or(p.host, |(h, _)| h);
        let connector = p
            .tls
            .connector()
            .await
            .map_err(|e| DialError::Msg(format!("tls: {e}")))?;
        let name = tls::server_name(hostname).map_err(|e| DialError::Msg(format!("tls: {e}")))?;
        match timeout_at(deadline, connector.connect(name, tcp)).await {
            Ok(Ok(s)) => Box::new(s),
            Ok(Err(e)) if p.tls.is_trust_failure(&e) => return Err(DialError::Cert),
            Ok(Err(e)) => {
                return Err(DialError::Msg(format!(
                    "tls: {}",
                    scrub(&io_err(&e), p.api_key)
                )))
            }
            Err(_) => return Err(DialError::Msg("tls: i/o timeout".into())),
        }
    } else {
        Box::new(tcp)
    };

    let mut nonce = [0u8; 16];
    getrandom::getrandom(&mut nonce)
        .map_err(|e| DialError::Msg(format!("upgrade: no randomness: {e}")))?;
    let key = crate::util::base64_encode(&nonce);
    let req = format!(
        "GET {} HTTP/1.1\r\nHost: {}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\
         Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {}\r\nX-API-KEY: {}\r\n\r\n",
        p.path, p.host, key, p.api_key
    );
    let up =
        |e: &std::io::Error| DialError::Msg(format!("upgrade: {}", scrub(&io_err(e), p.api_key)));
    match timeout_at(deadline, async {
        io.write_all(req.as_bytes()).await?;
        io.flush().await
    })
    .await
    {
        Ok(Ok(())) => {}
        Ok(Err(e)) => return Err(up(&e)),
        Err(_) => return Err(DialError::Msg("upgrade: i/o timeout".into())),
    }
    let mut head = Vec::new();
    let mut chunk = vec![0u8; 4096];
    let end = loop {
        if let Some(e) = find_head_end(&head) {
            break e;
        }
        if head.len() > MAX_HEAD {
            return Err(DialError::Msg("upgrade: response header too large".into()));
        }
        match timeout_at(deadline, io.read(&mut chunk)).await {
            Ok(Ok(0)) => return Err(DialError::Msg("upgrade: EOF".into())),
            Ok(Ok(n)) => head.extend_from_slice(&chunk[..n]),
            Ok(Err(e)) => return Err(up(&e)),
            Err(_) => return Err(DialError::Msg("upgrade: i/o timeout".into())),
        }
    };
    let Some((code, accept)) = parse_head(&head[..end]) else {
        return Err(DialError::Msg("upgrade: malformed HTTP response".into()));
    };
    if code != 101 {
        return Err(DialError::Status(code));
    }
    if accept != accept_for(&key) {
        return Err(DialError::Msg("upgrade: bad Sec-WebSocket-Accept".into()));
    }
    let mut dec = Decoder::new();
    dec.push(&head[end..]); // frames that arrived with the 101 stay in the stream
    Ok(WsConn { io, dec })
}

impl WsConn {
    /// A connection over any stream (tests): `leftover` are bytes already read past the head.
    pub fn from_io(io: Box<dyn Io>, leftover: &[u8]) -> Self {
        let mut dec = Decoder::new();
        dec.push(leftover);
        WsConn { io, dec }
    }

    /// `writeFrame`: one final, masked frame, within [`WRITE_TIMEOUT`].
    pub async fn write_frame(&mut self, op: u8, payload: &[u8]) -> std::io::Result<()> {
        let mut mask = [0u8; 4];
        getrandom::getrandom(&mut mask)
            .map_err(|e| std::io::Error::other(format!("no randomness: {e}")))?;
        let frame = encode_frame(op, payload, mask);
        match timeout(WRITE_TIMEOUT, async {
            self.io.write_all(&frame).await?;
            self.io.flush().await
        })
        .await
        {
            Ok(r) => r,
            Err(_) => Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "i/o timeout",
            )),
        }
    }

    /// `close`: a close frame (best effort), then the socket.
    pub async fn close(mut self) {
        let _ = self.write_frame(0x8, &[]).await;
        let _ = timeout(Duration::from_secs(1), self.io.shutdown()).await;
    }
}

/// The two intervals `readMessage` and the ping goroutine run on.
#[derive(Clone, Copy, Debug)]
pub struct PumpTiming {
    pub ping_interval: Duration,
    pub pong_wait: Duration,
}

/// Reads the connection until it fails or `cancel` fires, handing every complete text message
/// to `on_message`, answering pings, pinging on an interval, and cutting a console that is
/// silent for `pong_wait` (every complete frame restarts that wait). The connection is closed
/// (close frame, then the socket) before this returns, unless the console already closed or
/// the stream is dead. `on_message` must not panic (the caller guards it).
pub async fn pump<F: FnMut(&[u8])>(
    mut conn: WsConn,
    cancel: &CancelToken,
    t: PumpTiming,
    mut on_message: F,
) -> WsError {
    let mut ping = tokio::time::interval_at(Instant::now() + t.ping_interval, t.ping_interval);
    ping.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut deadline = Instant::now() + t.pong_wait;
    let mut chunk = vec![0u8; 16 << 10];
    let mut frames_seen = conn.dec.frames();
    let err = 'outer: loop {
        // Drain what is already buffered (the first pass handles bytes that came with the 101).
        loop {
            match conn.dec.next_event() {
                Ok(Some(Event::Message(m))) => on_message(&m),
                Ok(Some(Event::Ping(p))) => {
                    if let Err(e) = conn.write_frame(0xA, &p).await {
                        break 'outer WsError::Io(io_err(&e));
                    }
                }
                Ok(Some(Event::Pong)) => {}
                Ok(Some(Event::Close)) => {
                    let _ = conn.write_frame(0x8, &[]).await;
                    break 'outer WsError::Closed;
                }
                Ok(None) => break,
                Err(e) => break 'outer e,
            }
        }
        if conn.dec.frames() != frames_seen {
            frames_seen = conn.dec.frames();
            deadline = Instant::now() + t.pong_wait;
        }
        tokio::select! {
            biased;
            _ = cancel.cancelled() => break WsError::Cancelled,
            _ = tokio::time::sleep_until(deadline) => break WsError::Timeout,
            _ = ping.tick() => {
                if let Err(e) = conn.write_frame(0x9, &[]).await {
                    break WsError::Io(io_err(&e));
                }
            }
            r = conn.io.read(&mut chunk) => match r {
                Ok(0) => break conn.dec.eof_error(),
                Ok(n) => conn.dec.push(&chunk[..n]),
                Err(e) => break WsError::Io(io_err(&e)),
            },
        }
    };
    match err {
        WsError::Cancelled | WsError::Timeout | WsError::Protocol | WsError::TooBig => {
            conn.close().await
        }
        _ => {}
    }
    err
}
