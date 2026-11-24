//! `fakeconsole_test.go` and the `fakeProtect` of `unifiprotect_test.go`, for Rust: a fake
//! Protect console on 127.0.0.1 (never a real one). `FakeConsole` serves the public events
//! websocket (`GET /proxy/protect/integration/v1/subscribe/events`) over plain TCP or TLS with a
//! self-signed certificate; `FakeProtect` answers the two Integration API calls over plain HTTP.

// Each test crate that declares `mod common;` compiles every helper here and uses a subset, so an
// item unused in one crate is not dead in the suite (R1-202: a per-consumer split is the alternative).
#![allow(dead_code)]

use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU16, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt, ReadHalf, WriteHalf};
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio::time::Instant;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::unifiprotect::ws::{Io, WS_GUID};
use transport_rs::driver::unifiprotect::Timing;
use transport_rs::driver::EventSink;
use transport_rs::util::base64_encode as b64;

pub const TEST_KEY: &str = "SECRET-API-KEY";

/// `fast`: every ring timer shrunk so a test runs in milliseconds.
pub fn fast() -> Timing {
    Timing {
        ring_min_hold: Duration::from_millis(150),
        ring_max_hold: Duration::from_millis(600),
        ping_interval: Duration::from_millis(100),
        pong_wait: Duration::from_millis(400),
        backoff_min: Duration::from_millis(20),
        backoff_max: Duration::from_millis(80),
        ..Timing::default()
    }
}

/// `recSink`: records everything the driver reports, in Go's spelling.
#[derive(Default)]
pub struct RecSink {
    events: Mutex<Vec<String>>,
}

impl RecSink {
    pub fn new() -> Arc<RecSink> {
        Arc::new(RecSink::default())
    }
    fn add(&self, e: String) {
        self.events.lock().unwrap().push(e);
    }
    pub fn list(&self) -> Vec<String> {
        self.events.lock().unwrap().clone()
    }
    pub fn has(&self, prefix: &str) -> bool {
        self.list().iter().any(|e| e.starts_with(prefix))
    }
    pub fn count(&self, prefix: &str) -> usize {
        self.list().iter().filter(|e| e.starts_with(prefix)).count()
    }
}

impl EventSink for RecSink {
    fn ringing(&self, call_id: &str, device_id: &str) {
        self.add(format!("ringing {call_id} {device_id}"));
    }
    fn state_changed(&self, call_id: &str, state: &str) {
        self.add(format!("state {call_id} {state}"));
    }
    fn availability_changed(&self, device_id: &str, available: bool, reason: &str) {
        if available {
            self.add(format!("available {device_id}"));
        } else {
            self.add(format!("unavailable {device_id} {reason}"));
        }
    }
}

/// `waitFor`: the condition within 5 s, polled every 10 ms.
pub async fn wait_for(what: &str, mut cond: impl FnMut() -> bool) {
    let end = Instant::now() + Duration::from_secs(5);
    while Instant::now() < end {
        if cond() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("condition not reached in 5s: {what}");
}

/// The bytes of one server-to-client frame (unmasked).
pub fn raw_frame(b0: u8, payload: &[u8]) -> Vec<u8> {
    let mut out = vec![b0];
    match payload.len() {
        n if n < 126 => out.push(n as u8),
        n if n < 1 << 16 => {
            out.push(126);
            out.extend_from_slice(&(n as u16).to_be_bytes());
        }
        n => {
            out.push(127);
            out.extend_from_slice(&(n as u64).to_be_bytes());
        }
    }
    out.extend_from_slice(payload);
    out
}

pub fn frame(op: u8, payload: &[u8]) -> Vec<u8> {
    raw_frame(0x80 | op, payload)
}

/// Reads one HTTP request head (and returns the bytes after it).
pub async fn read_head(io: &mut dyn Io, limit: usize) -> Option<(String, Vec<u8>)> {
    let mut buf = Vec::new();
    let mut tmp = [0u8; 2048];
    loop {
        if let Some(i) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
            let rest = buf.split_off(i + 4);
            return Some((String::from_utf8_lossy(&buf).to_string(), rest));
        }
        if buf.len() > limit {
            return None;
        }
        let n = io.read(&mut tmp).await.ok()?;
        if n == 0 {
            return None;
        }
        buf.extend_from_slice(&tmp[..n]);
    }
}

pub fn header<'a>(head: &'a str, name: &str) -> Option<&'a str> {
    head.lines().skip(1).find_map(|l| {
        let (k, v) = l.split_once(':')?;
        k.trim().eq_ignore_ascii_case(name).then(|| v.trim())
    })
}

/// A self-signed certificate for 127.0.0.1: (rustls acceptor, the certificate in DER).
pub fn self_signed_acceptor() -> (
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

/// Like [`self_signed_acceptor`], but the certificate says it is version 1 (the `[0] version`
/// element holds 0): `rustls-webpki` refuses to read it, so the signature of the handshake cannot
/// be checked against it, whatever `verify_ssl` says. (Go's `crypto/x509` reads a v1 certificate.)
pub fn unreadable_cert_acceptor() -> tokio_rustls::TlsAcceptor {
    fn hdr(b: &[u8]) -> (usize, usize) {
        if b[1] < 0x80 {
            (2, b[1] as usize)
        } else {
            let n = (b[1] & 0x7f) as usize;
            let len = b[2..2 + n]
                .iter()
                .fold(0usize, |a, x| (a << 8) | *x as usize);
            (2 + n, len)
        }
    }
    let ck = rcgen::generate_simple_self_signed(vec!["127.0.0.1".to_string()]).unwrap();
    let mut der = ck.cert.der().to_vec();
    let mut off = hdr(&der).0; // into the Certificate
    off += hdr(&der[off..]).0; // into the TBSCertificate
                               // [0] { INTEGER 2 }: A0 03 02 01 02
    assert_eq!(
        &der[off..off + 5],
        &[0xA0, 0x03, 0x02, 0x01, 0x02],
        "the version element"
    );
    der[off + 4] = 0;
    // `with_single_cert` would parse the certificate and refuse it: hand it over unchecked
    #[derive(Debug)]
    struct Fixed(Arc<rustls::sign::CertifiedKey>);
    impl rustls::server::ResolvesServerCert for Fixed {
        fn resolve(
            &self,
            _: rustls::server::ClientHello<'_>,
        ) -> Option<Arc<rustls::sign::CertifiedKey>> {
            Some(self.0.clone())
        }
    }
    let provider = Arc::new(rustls::crypto::aws_lc_rs::default_provider());
    let key = rustls::pki_types::PrivateKeyDer::from(rustls::pki_types::PrivatePkcs8KeyDer::from(
        ck.signing_key.serialize_der(),
    ));
    let signer = provider.key_provider.load_private_key(key).unwrap();
    let certified =
        rustls::sign::CertifiedKey::new(vec![rustls::pki_types::CertificateDer::from(der)], signer);
    let cfg = rustls::ServerConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_no_client_auth()
        .with_cert_resolver(Arc::new(Fixed(Arc::new(certified))));
    tokio_rustls::TlsAcceptor::from(Arc::new(cfg))
}

/// The write half of a connection, shared by the test and the reader task; `None` once closed.
type SharedWriter = Arc<tokio::sync::Mutex<Option<WriteHalf<Box<dyn Io>>>>>;

struct Conn {
    w: SharedWriter,
    kill: CancelToken,
}

/// What the fake console records and does.
#[derive(Default)]
pub struct ConsoleState {
    pub keys: Vec<String>,
    pub paths: Vec<String>,
    pub heads: Vec<String>,
    pub attempts: usize,
    /// every frame the client sent: (opcode, payload)
    pub client_frames: Vec<(u8, Vec<u8>)>,
}

pub struct FakeConsole {
    pub addr: std::net::SocketAddr,
    pub cert: Option<rustls::pki_types::CertificateDer<'static>>,
    pub st: Arc<Mutex<ConsoleState>>,
    conns: Arc<Mutex<Vec<Conn>>>,
    status: Arc<AtomicU16>,
    no_pong: Arc<AtomicBool>,
    pub pings: Arc<AtomicI32>,
    connected: tokio::sync::Mutex<mpsc::UnboundedReceiver<()>>,
    kill: CancelToken,
}

impl Drop for FakeConsole {
    fn drop(&mut self) {
        self.kill.cancel();
        for c in self.conns.lock().unwrap().iter() {
            c.kill.cancel();
        }
    }
}

impl FakeConsole {
    pub async fn new(tls: bool) -> FakeConsole {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (acceptor, cert) = if tls {
            let (a, c) = self_signed_acceptor();
            (Some(a), Some(c))
        } else {
            (None, None)
        };
        let st = Arc::new(Mutex::new(ConsoleState::default()));
        let conns: Arc<Mutex<Vec<Conn>>> = Arc::new(Mutex::new(Vec::new()));
        let status = Arc::new(AtomicU16::new(0));
        let no_pong = Arc::new(AtomicBool::new(false));
        let pings = Arc::new(AtomicI32::new(0));
        let (ctx, crx) = mpsc::unbounded_channel();
        let kill = CancelToken::new();
        let (st2, conns2, status2, np2, pings2, kill2) = (
            st.clone(),
            conns.clone(),
            status.clone(),
            no_pong.clone(),
            pings.clone(),
            kill.clone(),
        );
        tokio::spawn(async move {
            loop {
                let tcp = tokio::select! {
                    _ = kill2.cancelled() => return,
                    r = listener.accept() => match r { Ok((s, _)) => s, Err(_) => return },
                };
                let (acceptor, st, conns, status, np, pings, ctx, kill) = (
                    acceptor.clone(),
                    st2.clone(),
                    conns2.clone(),
                    status2.clone(),
                    np2.clone(),
                    pings2.clone(),
                    ctx.clone(),
                    kill2.clone(),
                );
                tokio::spawn(async move {
                    let mut io: Box<dyn Io> = match acceptor {
                        Some(a) => match a.accept(tcp).await {
                            Ok(s) => Box::new(s),
                            Err(_) => return, // a client that refused the certificate
                        },
                        None => Box::new(tcp),
                    };
                    let Some((head, rest)) = read_head(&mut *io, 64 << 10).await else {
                        return;
                    };
                    let key = header(&head, "X-API-KEY").unwrap_or("").to_string();
                    let path = head
                        .lines()
                        .next()
                        .and_then(|l| l.split(' ').nth(1))
                        .unwrap_or("")
                        .to_string();
                    {
                        let mut g = st.lock().unwrap();
                        g.attempts += 1;
                        g.keys.push(key.clone());
                        g.paths.push(path.clone());
                        g.heads.push(head.clone());
                    }
                    let code = status.load(Ordering::SeqCst);
                    if code != 0 {
                        let body = "TOKEN-IN-ERROR-BODY";
                        let _ = io
                            .write_all(
                                format!("HTTP/1.1 {code} X\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len())
                                    .as_bytes(),
                            )
                            .await;
                        return;
                    }
                    if path != "/proxy/protect/integration/v1/subscribe/events" || key != TEST_KEY {
                        let _ = io
                            .write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                            .await;
                        return;
                    }
                    let up = header(&head, "Upgrade").unwrap_or("");
                    if !up.eq_ignore_ascii_case("websocket")
                        || header(&head, "Sec-WebSocket-Version") != Some("13")
                    {
                        let _ = io
                            .write_all(b"HTTP/1.1 426 Upgrade Required\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                            .await;
                        return;
                    }
                    let k = header(&head, "Sec-WebSocket-Key").unwrap_or("");
                    let d = aws_lc_rs::digest::digest(
                        &aws_lc_rs::digest::SHA1_FOR_LEGACY_USE_ONLY,
                        format!("{k}{WS_GUID}").as_bytes(),
                    );
                    let resp = format!(
                        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {}\r\n\r\n",
                        b64(d.as_ref())
                    );
                    if io.write_all(resp.as_bytes()).await.is_err() {
                        return;
                    }
                    let (r, w) = tokio::io::split(io);
                    let w = Arc::new(tokio::sync::Mutex::new(Some(w)));
                    let ckill = CancelToken::new();
                    conns.lock().unwrap().push(Conn {
                        w: w.clone(),
                        kill: ckill.clone(),
                    });
                    let _ = ctx.send(());
                    client_reader(r, rest, w, st, np, pings, ckill, kill).await;
                });
            }
        });
        FakeConsole {
            addr,
            cert,
            st,
            conns,
            status,
            no_pong,
            pings,
            connected: tokio::sync::Mutex::new(crx),
            kill,
        }
    }

    /// `host`: 127.0.0.1:port.
    pub fn host(&self) -> String {
        self.addr.to_string()
    }

    /// `lastConn`: waits for the next upgrade.
    pub async fn last_conn(&self) {
        let mut rx = self.connected.lock().await;
        tokio::time::timeout(Duration::from_secs(5), rx.recv())
            .await
            .expect("the driver never connected to the fake console")
            .expect("the console is gone");
    }

    /// Waits for a connection for at most `d`; true if one came.
    pub async fn connected_within(&self, d: Duration) -> bool {
        let mut rx = self.connected.lock().await;
        // `Ok(None)` is a closed channel (the fake's accept task is gone), not a connection
        matches!(tokio::time::timeout(d, rx.recv()).await, Ok(Some(_)))
    }

    async fn newest(&self) -> SharedWriter {
        self.conns
            .lock()
            .unwrap()
            .last()
            .expect("no connection")
            .w
            .clone()
    }

    /// `send`: one text frame (a JSON event) to the newest connection.
    pub async fn send(&self, msg: &str) {
        self.send_bytes(&frame(0x1, msg.as_bytes())).await;
    }

    /// `sendRaw`: a frame with an arbitrary first byte.
    pub async fn send_raw(&self, b0: u8, payload: &[u8]) {
        self.send_bytes(&raw_frame(b0, payload)).await;
    }

    /// Arbitrary bytes on the wire of the newest connection.
    pub async fn send_bytes(&self, bytes: &[u8]) {
        let w = self.newest().await;
        let mut g = w.lock().await;
        let w = g.as_mut().expect("connection closed");
        w.write_all(bytes).await.expect("send");
        let _ = w.flush().await;
    }

    /// Like `send_bytes` but a failed write is not an error (the client may have reset).
    pub async fn send_bytes_lossy(&self, bytes: &[u8]) {
        let w = self.newest().await;
        let mut g = w.lock().await;
        if let Some(w) = g.as_mut() {
            let _ = w.write_all(bytes).await;
            let _ = w.flush().await;
        }
    }

    /// `dropAll`: closes every connection.
    pub fn drop_all(&self) {
        for c in self.conns.lock().unwrap().drain(..) {
            c.kill.cancel();
        }
    }

    pub fn set_status(&self, s: u16) {
        self.status.store(s, Ordering::SeqCst);
    }
    pub fn set_no_pong(&self, b: bool) {
        self.no_pong.store(b, Ordering::SeqCst);
    }
    pub fn attempt_count(&self) -> usize {
        self.st.lock().unwrap().attempts
    }
    pub fn pings(&self) -> i32 {
        self.pings.load(Ordering::SeqCst)
    }
    pub fn keys(&self) -> Vec<String> {
        self.st.lock().unwrap().keys.clone()
    }
    pub fn paths(&self) -> Vec<String> {
        self.st.lock().unwrap().paths.clone()
    }
    pub fn client_frames(&self) -> Vec<(u8, Vec<u8>)> {
        self.st.lock().unwrap().client_frames.clone()
    }
    pub fn heads(&self) -> Vec<String> {
        self.st.lock().unwrap().heads.clone()
    }
}

/// `readClient`: answers pings and records everything else the client sends; ends when the
/// connection is killed or closed.
#[allow(clippy::too_many_arguments)]
async fn client_reader(
    mut r: ReadHalf<Box<dyn Io>>,
    mut buf: Vec<u8>,
    w: SharedWriter,
    st: Arc<Mutex<ConsoleState>>,
    no_pong: Arc<AtomicBool>,
    pings: Arc<AtomicI32>,
    ckill: CancelToken,
    kill: CancelToken,
) {
    let mut tmp = vec![0u8; 8192];
    loop {
        // one masked frame from `buf`
        while let Some((op, payload, used)) = parse_masked(&buf) {
            buf.drain(..used);
            st.lock().unwrap().client_frames.push((op, payload.clone()));
            match op {
                0x9 => {
                    pings.fetch_add(1, Ordering::SeqCst);
                    if !no_pong.load(Ordering::SeqCst) {
                        let mut g = w.lock().await;
                        if let Some(w) = g.as_mut() {
                            let _ = w.write_all(&frame(0xA, &payload)).await;
                        }
                    }
                }
                0x8 => {
                    let mut g = w.lock().await;
                    if let Some(mut w) = g.take() {
                        let _ = w.write_all(&frame(0x8, &payload)).await;
                        let _ = w.shutdown().await;
                    }
                    return;
                }
                _ => {}
            }
        }
        tokio::select! {
            _ = ckill.cancelled() => break,
            _ = kill.cancelled() => break,
            n = r.read(&mut tmp) => match n {
                Ok(0) | Err(_) => break,
                Ok(n) => buf.extend_from_slice(&tmp[..n]),
            },
        }
    }
    let mut g = w.lock().await;
    if let Some(mut w) = g.take() {
        let _ = w.shutdown().await;
    }
}

/// One client (masked) frame at the start of `b`: (opcode, unmasked payload, bytes used).
pub fn parse_masked(b: &[u8]) -> Option<(u8, Vec<u8>, usize)> {
    if b.len() < 2 {
        return None;
    }
    let (n, mut h) = match b[1] & 0x7f {
        126 => {
            if b.len() < 4 {
                return None;
            }
            (u16::from_be_bytes([b[2], b[3]]) as usize, 4)
        }
        127 => {
            if b.len() < 10 {
                return None;
            }
            let mut x = [0u8; 8];
            x.copy_from_slice(&b[2..10]);
            (u64::from_be_bytes(x) as usize, 10)
        }
        s => (s as usize, 2),
    };
    let masked = b[1] & 0x80 != 0;
    let mut mask = [0u8; 4];
    if masked {
        if b.len() < h + 4 {
            return None;
        }
        mask.copy_from_slice(&b[h..h + 4]);
        h += 4;
    }
    if b.len() < h + n {
        return None;
    }
    let mut p = b[h..h + n].to_vec();
    if masked {
        for (i, x) in p.iter_mut().enumerate() {
            *x ^= mask[i % 4];
        }
    }
    Some((b[0] & 0x0f, p, h + n))
}

// ---- the REST half -------------------------------------------------------------------------

#[derive(Default)]
pub struct ProtectState {
    /// "METHOD path"
    pub requests: Vec<String>,
    pub keys: Vec<String>,
    pub bodies: Vec<String>,
    pub heads: Vec<String>,
    /// value for the "high" stream
    pub rtsps: String,
    /// JSON for talkback-session
    pub talkback: String,
    /// non-zero forces this status on every reply
    pub status: u16,
    /// when set, replaces the whole answer to the next request (raw bytes), then clears
    pub raw_reply: Option<Vec<u8>>,
    /// answers nothing: the request is read and the connection kept open
    pub silent: bool,
}

pub struct FakeProtect {
    pub addr: std::net::SocketAddr,
    pub st: Arc<Mutex<ProtectState>>,
    kill: CancelToken,
}

impl Drop for FakeProtect {
    fn drop(&mut self) {
        self.kill.cancel();
    }
}

impl FakeProtect {
    pub async fn new() -> FakeProtect {
        Self::with_tls(None).await
    }

    pub async fn with_tls(acceptor: Option<tokio_rustls::TlsAcceptor>) -> FakeProtect {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let st = Arc::new(Mutex::new(ProtectState {
            rtsps: "rtsps://nvr.example:7441/TOKEN123?enableSrtp".into(),
            talkback: r#"{"url":"rtp://192.168.2.9:7004","codec":"opus","samplingRate":24000,"bitsPerSample":16}"#.into(),
            ..Default::default()
        }));
        let kill = CancelToken::new();
        let (st2, kill2) = (st.clone(), kill.clone());
        tokio::spawn(async move {
            loop {
                let tcp = tokio::select! {
                    _ = kill2.cancelled() => return,
                    r = listener.accept() => match r { Ok((s, _)) => s, Err(_) => return },
                };
                let (st, acceptor, kill) = (st2.clone(), acceptor.clone(), kill2.clone());
                tokio::spawn(async move {
                    let mut io: Box<dyn Io> = match acceptor {
                        Some(a) => match a.accept(tcp).await {
                            Ok(s) => Box::new(s),
                            Err(_) => return,
                        },
                        None => Box::new(tcp),
                    };
                    let Some((head, mut rest)) = read_head(&mut *io, 64 << 10).await else {
                        return;
                    };
                    let cl: usize = header(&head, "Content-Length")
                        .and_then(|v| v.parse().ok())
                        .unwrap_or(0);
                    let mut tmp = [0u8; 1024];
                    while rest.len() < cl {
                        match io.read(&mut tmp).await {
                            Ok(n) if n > 0 => rest.extend_from_slice(&tmp[..n]),
                            _ => break,
                        }
                    }
                    let line = head.lines().next().unwrap_or("").to_string();
                    let mut parts = line.split(' ');
                    let (method, path) = (parts.next().unwrap_or(""), parts.next().unwrap_or(""));
                    let (reply, silent) = {
                        let mut g = st.lock().unwrap();
                        g.requests.push(format!("{method} {path}"));
                        g.keys
                            .push(header(&head, "X-API-KEY").unwrap_or("").to_string());
                        g.bodies.push(String::from_utf8_lossy(&rest).to_string());
                        g.heads.push(head.clone());
                        let reply = if let Some(r) = g.raw_reply.take() {
                            r
                        } else if g.status != 0 {
                            let b = "TOKEN-IN-ERROR-BODY";
                            format!("HTTP/1.1 {} X\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{b}", g.status, b.len()).into_bytes()
                        } else if path.ends_with("/rtsps-stream") {
                            let mut m = serde_json::Map::new();
                            m.insert("high".into(), g.rtsps.clone().into());
                            let b = serde_json::to_string(&m).unwrap() + "\n";
                            format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{b}", b.len()).into_bytes()
                        } else if path.ends_with("/talkback-session") {
                            let b = g.talkback.clone();
                            format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{b}", b.len()).into_bytes()
                        } else {
                            b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_vec()
                        };
                        (reply, g.silent)
                    };
                    if silent {
                        kill.cancelled().await;
                        return;
                    }
                    let _ = io.write_all(&reply).await;
                    let _ = io.flush().await;
                    let _ = io.shutdown().await;
                });
            }
        });
        FakeProtect { addr, st, kill }
    }

    pub fn host(&self) -> String {
        self.addr.to_string()
    }
    pub fn reqs(&self) -> Vec<String> {
        self.st.lock().unwrap().requests.clone()
    }
    pub fn keys(&self) -> Vec<String> {
        self.st.lock().unwrap().keys.clone()
    }
    pub fn bodies(&self) -> Vec<String> {
        self.st.lock().unwrap().bodies.clone()
    }
    pub fn heads(&self) -> Vec<String> {
        self.st.lock().unwrap().heads.clone()
    }
    pub fn set_rtsps(&self, v: &str) {
        self.st.lock().unwrap().rtsps = v.into();
    }
    pub fn set_talkback(&self, v: &str) {
        self.st.lock().unwrap().talkback = v.into();
    }
    pub fn set_status(&self, s: u16) {
        self.st.lock().unwrap().status = s;
    }
    pub fn set_raw_reply(&self, b: &[u8]) {
        self.st.lock().unwrap().raw_reply = Some(b.to_vec());
    }
    pub fn set_silent(&self, b: bool) {
        self.st.lock().unwrap().silent = b;
    }
}
