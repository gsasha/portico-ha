//! `api.go`: a minimal client for the two calls of the Protect console's public Integration API
//! this driver needs, authenticated with the `X-API-KEY` header (there is no login, cookie or CSRF
//! token in this API; the shapes were read from uiprotect's source in Go, not from a live
//! console).
//!
//! Go uses `net/http`; transport-rs has no HTTP client crate, so this is the smallest client that
//! does what the Go one does here: one POST per call over a fresh connection (TLS through
//! [`Tls`]), no redirect followed (a 3xx is a status), the whole exchange capped at 10 s, at
//! most 64 KiB of the body read (the rest is ignored, as `io.LimitReader` ignores it), and
//! errors that never carry the API key or the response body, which can hold stream tokens.
//!
//! Differences from Go (docs/parity/unifi_protect.md 108, 109, 122, 123): no connection reuse and no gzip (the request does
//! not offer it), the `User-Agent` names this binary, and the TEXT of a transport failure
//! (`dial tcp ...`, `tls: ...`) is a Go-shaped approximation: its form and the key scrubbing are
//! Go's, its wording after the URL is not byte for byte.

use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::{timeout_at, Instant};

use super::ws::{scrub, Io};
use crate::cancel::CancelToken;
use crate::gojson::{self, io_err, Kind, Spec};
use crate::gostd::http1::find_head_end;
use crate::gostd::tls::{self, Tls};
use crate::gostd::url as gourl;

pub const API_BASE: &str = "/proxy/protect/integration/v1/cameras/";
pub const MAX_API_RESPONSE: usize = 64 << 10;
pub const API_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_HEAD: usize = 1 << 20;
const USER_AGENT: &str = concat!("portico-transport-rs/", env!("CARGO_PKG_VERSION"));

/// `url.PathEscape`: everything but letters, digits, `-_.~` and `$&+=:@` is percent-encoded
/// (so `/ ; , ?` and every other byte are).
pub fn path_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"-_.~$&+=:@".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// `talkbackSession`: the camera-side RTP sink Protect opens for a talkback.
///
/// `Debug` is written by hand and prints the URL as `<redacted>` (docs/parity/unifi_protect.md 133): the
/// URL is the console's RTP sink and can carry a token, and a derived `{:?}` would put it in a
/// log or a panic text.
#[derive(Clone, Default, PartialEq, Eq)]
pub struct TalkbackSession {
    pub url: String,
    pub codec: String,
    pub sampling_rate: i64,
    pub bits_per_sample: i64,
}

impl std::fmt::Debug for TalkbackSession {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TalkbackSession")
            .field("url", &"<redacted>")
            .field("codec", &self.codec)
            .field("sampling_rate", &self.sampling_rate)
            .field("bits_per_sample", &self.bits_per_sample)
            .finish()
    }
}

/// `protectAPI`.
pub struct ProtectApi {
    scheme: String,
    host: String,
    api_key: String,
    tls: std::sync::Arc<Tls>,
    timeout: Duration,
}

/// Where a request failed, for the wording of a timeout.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Phase {
    Headers,
    Body,
}

impl ProtectApi {
    /// `newProtectAPI(scheme+"://"+host, key, verify)`.
    pub fn new(scheme: &str, host: &str, api_key: &str, verify_ssl: bool) -> Self {
        ProtectApi {
            scheme: scheme.to_string(),
            host: host.to_string(),
            api_key: api_key.to_string(),
            tls: std::sync::Arc::new(Tls::new(verify_ssl)),
            timeout: API_TIMEOUT,
        }
    }

    /// A client that uses `tls` (tests that trust a given certificate).
    #[cfg(feature = "test-seams")]
    pub fn with_tls(scheme: &str, host: &str, api_key: &str, tls: std::sync::Arc<Tls>) -> Self {
        let mut a = ProtectApi::new(scheme, host, api_key, true);
        a.tls = tls;
        a
    }

    /// The TLS state, shared with the websocket.
    pub fn tls(&self) -> std::sync::Arc<Tls> {
        self.tls.clone()
    }

    #[cfg(feature = "test-seams")]
    pub fn set_timeout(&mut self, d: Duration) {
        self.timeout = d;
    }

    fn url(&self, path: &str) -> String {
        format!("{}://{}{}", self.scheme, self.host, path)
    }

    /// `do`: one request, the (size-capped) body of a 2xx answer.
    pub async fn request(
        &self,
        cancel: &CancelToken,
        method: &str,
        path: &str,
        body: Option<&[u8]>,
    ) -> Result<Vec<u8>, String> {
        let deadline = Instant::now() + self.timeout;
        let mut phase = Phase::Headers;
        let r = tokio::select! {
            _ = cancel.cancelled() => Err(Fail::Do("context canceled".to_string())),
            r = timeout_at(deadline, self.exchange(method, path, body, &mut phase)) => match r {
                Ok(r) => r,
                Err(_) => Err(match phase {
                    Phase::Headers => Fail::Do("context deadline exceeded (Client.Timeout exceeded while awaiting headers)".to_string()),
                    Phase::Body => Fail::Read("context deadline exceeded (Client.Timeout or context cancellation while reading body)".to_string()),
                }),
            },
        };
        match r {
            Ok((status, data)) => {
                if !(200..=299).contains(&status) {
                    return Err(format!("{method} {path}: HTTP {status}"));
                }
                Ok(data)
            }
            Err(Fail::Do(cause)) => {
                let op = {
                    let m = method.to_ascii_lowercase();
                    let mut c = m.chars();
                    c.next()
                        .map(|f| f.to_ascii_uppercase().to_string() + c.as_str())
                        .unwrap_or_default()
                };
                Err(scrub(
                    &format!("{method} {path}: {op} \"{}\": {cause}", self.url(path)),
                    &self.api_key,
                ))
            }
            Err(Fail::Bare(text)) => Err(scrub(&text, &self.api_key)),
            Err(Fail::Read(cause)) => Err(scrub(
                &format!("{method} {path}: reading response: {cause}"),
                &self.api_key,
            )),
        }
    }

    async fn exchange(
        &self,
        method: &str,
        path: &str,
        body: Option<&[u8]>,
        phase: &mut Phase,
    ) -> Result<(u16, Vec<u8>), Fail> {
        if !super::ws::header_safe(&self.host, false)
            || !super::ws::header_safe(&self.api_key, true)
        {
            return Err(Fail::Do(
                "refusing a host or key with control characters".into(),
            ));
        }
        // `http.NewRequest(baseURL+path)` and `net/http`: the URL is parsed (a bad one is the
        // bare parse error, as `do` returns it), the dial target is `Hostname()` and `Port()` with
        // the scheme's default for an absent or empty port, the `Host` header drops an empty port
        // (R2-7; the websocket side dials `net.SplitHostPort(host)` instead: ws.rs)
        let u = gourl::parse(&self.url(path)).map_err(Fail::Bare)?;
        let addr = u.dial_addr(if self.scheme == "https" { "443" } else { "80" });
        let host_header = u.host_header();
        let tcp = TcpStream::connect(addr.as_str())
            .await
            .map_err(|e| Fail::Do(format!("dial tcp {addr}: connect: {}", io_err(&e))))?;
        let _ = tcp.set_nodelay(true);
        let mut io: Box<dyn Io> = if self.scheme == "https" {
            let (hostname, _) = u.request_host_port();
            let connector = self
                .tls
                .connector()
                .await
                .map_err(|e| Fail::Do(format!("tls: {e}")))?;
            let name = tls::server_name(&hostname).map_err(|e| Fail::Do(format!("tls: {e}")))?;
            match connector.connect(name, tcp).await {
                Ok(s) => Box::new(s),
                Err(e) if self.tls.is_trust_failure(&e) => {
                    return Err(Fail::Do(format!(
                        "tls: failed to verify certificate: {}",
                        tls::cert_error_text(&e)
                    )))
                }
                Err(e) => return Err(Fail::Do(format!("tls: {}", io_err(&e)))),
            }
        } else {
            Box::new(tcp)
        };

        let mut req = format!(
            "{method} {path} HTTP/1.1\r\nHost: {host_header}\r\nUser-Agent: {USER_AGENT}\r\nX-API-KEY: {}\r\nAccept: application/json\r\n",
            self.api_key
        );
        match body {
            Some(b) => req.push_str(&format!(
                "Content-Type: application/json\r\nContent-Length: {}\r\n",
                b.len()
            )),
            None if method == "POST" || method == "PUT" || method == "PATCH" => {
                req.push_str("Content-Length: 0\r\n")
            }
            None => {}
        }
        req.push_str("Connection: close\r\n\r\n");
        let mut out = req.into_bytes();
        if let Some(b) = body {
            out.extend_from_slice(b);
        }
        io.write_all(&out)
            .await
            .map_err(|e| Fail::Do(format!("write tcp {addr}: {}", io_err(&e))))?;
        io.flush()
            .await
            .map_err(|e| Fail::Do(format!("write tcp {addr}: {}", io_err(&e))))?;

        // The head, skipping interim 1xx answers.
        let mut buf: Vec<u8> = Vec::new();
        let mut chunk = vec![0u8; 8192];
        let (status, framing) = loop {
            let end = loop {
                if let Some(e) = find_head_end(&buf) {
                    break e;
                }
                if buf.len() > MAX_HEAD {
                    return Err(Fail::Do(
                        "http: server response headers exceeded the limit".into(),
                    ));
                }
                let n = io
                    .read(&mut chunk)
                    .await
                    .map_err(|e| Fail::Do(io_err(&e)))?;
                if n == 0 {
                    return Err(Fail::Do(if buf.is_empty() {
                        "EOF".into()
                    } else {
                        "unexpected EOF".into()
                    }));
                }
                buf.extend_from_slice(&chunk[..n]);
            };
            let head: Vec<u8> = buf.drain(..end).collect();
            let (status, framing) =
                parse_head(&head).ok_or_else(|| Fail::Do("malformed HTTP response".into()))?;
            if (100..200).contains(&status) && status != 101 {
                continue;
            }
            break (status, framing);
        };
        *phase = Phase::Body;
        let data = if method == "HEAD" || status == 204 || status == 304 {
            Vec::new()
        } else {
            read_body(&mut *io, buf, framing, MAX_API_RESPONSE)
                .await
                .map_err(Fail::Read)?
        };
        Ok((status, data))
    }

    /// `RTSPSURL`: mints (POST) the camera's RTSPS share for one quality and returns its URL,
    /// which embeds the per-camera stream token. Minting is idempotent on the console.
    pub async fn rtsps_url(
        &self,
        cancel: &CancelToken,
        camera_id: &str,
        quality: &str,
    ) -> Result<String, String> {
        let mut body = String::from("{\"qualities\":[");
        gojson::push_string(&mut body, quality);
        body.push_str("]}");
        let data = self
            .request(
                cancel,
                "POST",
                &format!("{API_BASE}{}/rtsps-stream", path_escape(camera_id)),
                Some(body.as_bytes()),
            )
            .await?;
        let Some(streams) = decode_string_map(&data) else {
            return Err("rtsps-stream: undecodable response".to_string());
        };
        let u = streams
            .iter()
            .rev()
            .find(|(k, _)| k == quality)
            .map(|(_, v)| v.clone())
            .unwrap_or_default();
        if u.is_empty() {
            return Err(format!(
                "rtsps-stream: no {} stream in the response",
                crate::gostd::strconv::go_quote(quality)
            ));
        }
        if !u.starts_with("rtsps://") {
            return Err("rtsps-stream: unexpected stream scheme".to_string());
        }
        Ok(u)
    }

    /// `TalkbackSession`: opens a talkback session (POST) and returns the RTP sink.
    pub async fn talkback_session(
        &self,
        cancel: &CancelToken,
        camera_id: &str,
    ) -> Result<TalkbackSession, String> {
        let data = self
            .request(
                cancel,
                "POST",
                &format!("{API_BASE}{}/talkback-session", path_escape(camera_id)),
                None,
            )
            .await?;
        decode_talkback_session(&data)
    }
}

/// `json.Unmarshal(data, &talkbackSession{})`: the error is Go's `undecodable response`.
pub(crate) fn decode_talkback_session(data: &[u8]) -> Result<TalkbackSession, String> {
    const SPECS: [Spec; 4] = [
        Spec {
            json: "url",
            kind: Kind::Str,
        },
        Spec {
            json: "codec",
            kind: Kind::Str,
        },
        Spec {
            json: "samplingRate",
            kind: Kind::Int,
        },
        Spec {
            json: "bitsPerSample",
            kind: Kind::Int,
        },
    ];
    let text = String::from_utf8_lossy(data);
    let v = gojson::decode(&text, "talkbackSession", &SPECS)
        .map_err(|_| "talkback-session: undecodable response".to_string())?;
    Ok(TalkbackSession {
        url: gojson::str_at(&v, 0),
        codec: gojson::str_at(&v, 1),
        sampling_rate: v
            .get(2)
            .and_then(|x| x.as_ref())
            .and_then(|x| x.as_i64())
            .unwrap_or(0),
        bits_per_sample: v
            .get(3)
            .and_then(|x| x.as_ref())
            .and_then(|x| x.as_i64())
            .unwrap_or(0),
    })
}

enum Fail {
    /// an error of `client.Do`
    Do(String),
    /// an error reading the body
    Read(String),
    /// the URL does not parse: `http.NewRequest`'s error, returned bare
    Bare(String),
}

/// `json.Unmarshal(data, &map[string]string)`: `None` when Go returns an error; the pairs in
/// order (a later duplicate wins when looked up from the end).
fn decode_string_map(data: &[u8]) -> Option<Vec<(String, String)>> {
    // `json.Unmarshal` repairs invalid UTF-8 inside a string and sees a syntax error anywhere
    // else, which U+FFFD is too; JSON white space is four characters
    let text = String::from_utf8_lossy(data);
    let text = crate::gostd::text::trim_json_space(&text);
    if text == "null" {
        return Some(Vec::new());
    }
    let entries = crate::ipc::parse_object_entries(text.as_bytes())?;
    let mut out = Vec::new();
    for (k, raw) in entries {
        let t = raw.get();
        if t == "null" {
            out.push((k, String::new()));
        } else {
            out.push((k, serde_json::from_str::<String>(t).ok()?));
        }
    }
    Some(out)
}

enum Framing {
    Chunked,
    Length(u64),
    Close,
}

fn parse_head(head: &[u8]) -> Option<(u16, Framing)> {
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
    let status: u16 = code.parse().ok()?;
    let mut chunked = false;
    let mut lengths: Vec<&str> = Vec::new();
    for l in lines {
        if l.is_empty() {
            break;
        }
        let Some((k, v)) = l.split_once(':') else {
            continue;
        };
        let (k, v) = (k.trim(), v.trim());
        if k.eq_ignore_ascii_case("transfer-encoding") && v.to_ascii_lowercase().contains("chunked")
        {
            chunked = true;
        } else if k.eq_ignore_ascii_case("content-length") {
            lengths.push(v);
        }
    }
    // net/http (`fixLength`): two different lengths are an error; so is one that is not a plain
    // non-negative integer of at most 63 bits, unless the body is chunked (R2-76)
    if lengths.iter().any(|v| *v != lengths[0]) {
        return None;
    }
    let mut length: Option<u64> = None;
    if !chunked {
        if let Some(v) = lengths.first() {
            match v.parse::<u64>() {
                Ok(n) if v.bytes().all(|b| b.is_ascii_digit()) && n <= i64::MAX as u64 => {
                    length = Some(n)
                }
                _ if v.is_empty() => {}
                _ => return None,
            }
        }
    }
    let framing = if chunked {
        Framing::Chunked
    } else if let Some(n) = length {
        Framing::Length(n)
    } else {
        Framing::Close
    };
    Some((status, framing))
}

/// At most `cap` body bytes (the rest is ignored), or the text of the read error.
async fn read_body(
    io: &mut dyn Io,
    mut buf: Vec<u8>,
    framing: Framing,
    cap: usize,
) -> Result<Vec<u8>, String> {
    let mut out: Vec<u8> = Vec::new();
    let mut tmp = vec![0u8; 8192];
    match framing {
        Framing::Length(n) => {
            let want = (n.min(cap as u64)) as usize;
            while out.len() < want {
                if buf.is_empty() {
                    let k = io.read(&mut tmp).await.map_err(|e| io_err(&e))?;
                    if k == 0 {
                        return Err("unexpected EOF".to_string());
                    }
                    buf.extend_from_slice(&tmp[..k]);
                }
                let take = buf.len().min(want - out.len());
                out.extend(buf.drain(..take));
            }
        }
        Framing::Close => {
            while out.len() < cap {
                if buf.is_empty() {
                    let k = io.read(&mut tmp).await.map_err(|e| io_err(&e))?;
                    if k == 0 {
                        break;
                    }
                    buf.extend_from_slice(&tmp[..k]);
                }
                let take = buf.len().min(cap - out.len());
                out.extend(buf.drain(..take));
            }
        }
        Framing::Chunked => {
            loop {
                // the size line
                let line = loop {
                    if let Some(i) = buf.iter().position(|b| *b == b'\n') {
                        let l: Vec<u8> = buf.drain(..=i).collect();
                        break l;
                    }
                    if buf.len() > 4096 {
                        return Err("http: chunk size line too long".to_string());
                    }
                    let k = io.read(&mut tmp).await.map_err(|e| io_err(&e))?;
                    if k == 0 {
                        return Err("unexpected EOF".to_string());
                    }
                    buf.extend_from_slice(&tmp[..k]);
                };
                let s = String::from_utf8_lossy(&line);
                let hex = s.trim().split(';').next().unwrap_or("").trim();
                let size = if hex.starts_with('+') {
                    Err(())
                } else {
                    u64::from_str_radix(hex, 16).map_err(|_| ())
                }
                .map_err(|_| "invalid byte in chunk length".to_string())?;
                if size == 0 {
                    break;
                }
                let mut left = size;
                while left > 0 {
                    if buf.is_empty() {
                        let k = io.read(&mut tmp).await.map_err(|e| io_err(&e))?;
                        if k == 0 {
                            return Err("unexpected EOF".to_string());
                        }
                        buf.extend_from_slice(&tmp[..k]);
                    }
                    let take = (buf.len() as u64).min(left) as usize;
                    let room = cap.saturating_sub(out.len());
                    out.extend(buf.drain(..take).take(room));
                    left -= take as u64;
                    if out.len() >= cap {
                        return Ok(out);
                    }
                }
                // the CRLF after the chunk (R2-76: `malformed chunked encoding` when it is not)
                while buf.len() < 2 {
                    let k = io.read(&mut tmp).await.map_err(|e| io_err(&e))?;
                    if k == 0 {
                        return Err("unexpected EOF".to_string());
                    }
                    buf.extend_from_slice(&tmp[..k]);
                }
                if buf[..2] != *b"\r\n" {
                    return Err("malformed chunked encoding".to_string());
                }
                buf.drain(..2);
            }
        }
    }
    Ok(out)
}
