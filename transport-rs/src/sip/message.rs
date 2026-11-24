//! Port of `message.go` and the request parser of `endpoint.go`: SIP message parsing and the
//! request builders. Parsing is deliberately as lenient as Go's: no folding, no compact forms,
//! Content-Length ignored, header lines without a colon skipped.

use std::collections::HashMap;

use super::Error;
use std::fmt::Write as _;

use crate::gostd::strconv::{go_atoi_bytes, go_quote_bytes as quote};
use crate::gostd::text::{go_lossy, go_to_lower};

/// A parsed SIP response. `headers` maps the lower-cased name to the raw values in arrival order.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Response {
    pub status_code: i64,
    pub reason: String,
    pub headers: HashMap<String, Vec<String>>,
    /// The same values as bytes, untouched by UTF-8 repair (for echoing).
    pub raw_headers: HashMap<String, Vec<Vec<u8>>>,
    pub body: Vec<u8>,
}

/// A parsed inbound SIP request.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ParsedRequest {
    pub method: String,
    pub uri: String,
    /// The request URI as received, bytes untouched (for echoing in ACK/CANCEL).
    pub raw_uri: Vec<u8>,
    pub headers: HashMap<String, Vec<String>>,
    /// The same values as bytes, untouched by UTF-8 repair (for echoing).
    pub raw_headers: HashMap<String, Vec<Vec<u8>>>,
    pub body: Vec<u8>,
}

fn first_header<'a>(h: &'a HashMap<String, Vec<String>>, name: &str) -> &'a str {
    h.get(&go_to_lower(name))
        .and_then(|v| v.first())
        .map(String::as_str)
        .unwrap_or("")
}

fn first_raw<'a>(h: &'a HashMap<String, Vec<Vec<u8>>>, name: &str) -> &'a [u8] {
    h.get(&go_to_lower(name))
        .and_then(|v| v.first())
        .map(Vec::as_slice)
        .unwrap_or(b"")
}

impl Response {
    /// First value of a header, case-insensitively, or `""`.
    pub fn header(&self, name: &str) -> &str {
        first_header(&self.headers, name)
    }

    /// Like [`Response::header`], as the bytes received.
    pub fn header_raw(&self, name: &str) -> &[u8] {
        first_raw(&self.raw_headers, name)
    }
}

impl ParsedRequest {
    /// First value of a header, case-insensitively, or `""`.
    pub fn header(&self, name: &str) -> &str {
        first_header(&self.headers, name)
    }

    /// Like [`ParsedRequest::header`], as the bytes received.
    pub fn header_raw(&self, name: &str) -> &[u8] {
        first_raw(&self.raw_headers, name)
    }

    /// All values of a header as received.
    pub fn headers_raw(&self, name: &str) -> &[Vec<u8>] {
        self.raw_headers
            .get(&go_to_lower(name))
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }
}

/// `strings.TrimSpace` on bytes: strips leading/trailing Unicode white space, stops at any byte
/// that is not valid UTF-8.
pub(super) fn trim_space_bytes(mut b: &[u8]) -> &[u8] {
    while let Some(c) = (1..=4.min(b.len()))
        .find_map(|l| std::str::from_utf8(&b[..l]).ok())
        .and_then(|s| s.chars().next())
        .filter(|c| c.is_whitespace())
    {
        b = &b[c.len_utf8()..];
    }
    while let Some(c) = (1..=4.min(b.len()))
        .find_map(|l| std::str::from_utf8(&b[b.len() - l..]).ok())
        .and_then(|s| s.chars().next_back())
        .filter(|c| c.is_whitespace())
    {
        b = &b[..b.len() - c.len_utf8()];
    }
    b
}

/// `bufio.Reader.ReadString('\n')` on a byte slice: the next line including its `\n`, and whether
/// the read hit EOF without one (Go's `io.EOF`).
fn read_line<'a>(raw: &'a [u8], pos: &mut usize) -> (&'a [u8], bool) {
    let rest = &raw[*pos..];
    match rest.iter().position(|&b| b == b'\n') {
        Some(i) => {
            *pos += i + 1;
            (&rest[..=i], false)
        }
        None => {
            *pos = raw.len();
            (rest, true)
        }
    }
}

/// `strings.TrimRight(line, "\r\n")`.
fn trim_crlf(line: &[u8]) -> &[u8] {
    let mut end = line.len();
    while end > 0 && (line[end - 1] == b'\r' || line[end - 1] == b'\n') {
        end -= 1;
    }
    &line[..end]
}

/// The header loop shared by both parsers. Returns whether the input ended inside the headers
/// (EOF with no body to read).
fn parse_headers(
    raw: &[u8],
    pos: &mut usize,
    headers: &mut HashMap<String, Vec<String>>,
    raw_headers: &mut HashMap<String, Vec<Vec<u8>>>,
) -> bool {
    loop {
        let (line, eof) = read_line(raw, pos);
        let trimmed = trim_crlf(line);
        if trimmed.is_empty() {
            return false; // end of headers; the body (if any) follows
        }
        let text = go_lossy(trimmed);
        if let Some(idx) = text.find(':') {
            let name = go_to_lower(text[..idx].trim());
            let value = text[idx + 1..].trim().to_string();
            if let Some(ri) = trimmed.iter().position(|&c| c == b':') {
                raw_headers
                    .entry(name.clone())
                    .or_default()
                    .push(trim_space_bytes(&trimmed[ri + 1..]).to_vec());
            }
            headers.entry(name).or_default().push(value);
        }
        if eof {
            return true;
        }
    }
}

/// `ParseResponse`.
pub fn parse_response(raw: &[u8]) -> Result<Response, Error> {
    let mut pos = 0;
    let (line, eof) = read_line(raw, &mut pos);
    if eof {
        return Err(Error::msg("sip: reading status line: EOF"));
    }
    // the error texts quote the bytes received (`\xNN` for an invalid one, as Go's %q does)
    let line = trim_crlf(line);
    let status_line = go_lossy(line);
    let parts: Vec<&str> = status_line.splitn(3, ' ').collect();
    let raw_parts: Vec<&[u8]> = line.splitn(3, |&c| c == b' ').collect();
    if parts.len() < 3 || !parts[0].starts_with("SIP/2.0") {
        return Err(Error::msg(format!(
            "sip: malformed status line {}",
            quote(line)
        )));
    }
    let code = go_atoi_bytes(raw_parts[1]).map_err(|e| {
        Error::msg(format!(
            "sip: malformed status code {}: {}",
            quote(raw_parts[1]),
            e
        ))
    })?;
    let mut resp = Response {
        status_code: code,
        reason: parts[2].to_string(),
        ..Default::default()
    };
    if parse_headers(raw, &mut pos, &mut resp.headers, &mut resp.raw_headers) {
        return Ok(resp); // EOF with no body
    }
    resp.body = raw[pos..].to_vec();
    Ok(resp)
}

/// `ParseRequest`.
pub fn parse_request(raw: &[u8]) -> Result<ParsedRequest, Error> {
    let mut pos = 0;
    let (line, eof) = read_line(raw, &mut pos);
    if eof {
        return Err(Error::msg("sip: reading request line: EOF"));
    }
    let request_line = go_lossy(trim_crlf(line));
    let parts: Vec<&str> = request_line.splitn(3, ' ').collect();
    if parts.len() < 3 || !parts[2].starts_with("SIP/2.0") {
        return Err(Error::msg(format!(
            "sip: malformed request line {}",
            quote(trim_crlf(line))
        )));
    }
    let mut req = ParsedRequest {
        method: parts[0].to_string(),
        uri: parts[1].to_string(),
        raw_uri: trim_crlf(line)
            .splitn(3, |&c| c == b' ')
            .nth(1)
            .unwrap_or(b"")
            .to_vec(),
        ..Default::default()
    };
    if parse_headers(raw, &mut pos, &mut req.headers, &mut req.raw_headers) {
        return Ok(req);
    }
    req.body = raw[pos..].to_vec();
    Ok(req)
}

/// The first position of `needle` in `hay`.
pub(crate) fn find_sub(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

// ---- request builders -------------------------------------------------------------------------

/// What one REGISTER needs. `auth_header` is the Authorization header VALUE as bytes (a digest
/// echoes the challenge's realm/nonce as received, which need not be UTF-8), empty for the first
/// attempt; `branch` includes the `z9hG4bK` prefix.
#[derive(Debug, Clone, Default)]
pub struct RegisterParams {
    pub host: String,
    pub local_addr: String,
    pub username: String,
    pub call_id: String,
    pub from_tag: String,
    pub cseq: i64,
    pub expires: i64,
    pub auth_header: Vec<u8>,
    pub branch: String,
}

pub fn build_register(p: &RegisterParams) -> Vec<u8> {
    let aor = format!("sip:{}@{}", p.username, p.host);
    let mut b = String::new();
    let _ = write!(b, "REGISTER sip:{} SIP/2.0\r\n", p.host);
    let _ = write!(
        b,
        "Via: SIP/2.0/UDP {};branch={}\r\n",
        p.local_addr, p.branch
    );
    b.push_str("Max-Forwards: 70\r\n");
    let _ = write!(b, "From: <{}>;tag={}\r\n", aor, p.from_tag);
    let _ = write!(b, "To: <{aor}>\r\n");
    let _ = write!(b, "Call-ID: {}\r\n", p.call_id);
    let _ = write!(b, "CSeq: {} REGISTER\r\n", p.cseq);
    let _ = write!(b, "Contact: <sip:{}@{}>\r\n", p.username, p.local_addr);
    let _ = write!(b, "Expires: {}\r\n", p.expires);
    let mut out = b.into_bytes();
    if !p.auth_header.is_empty() {
        out.extend_from_slice(b"Authorization: ");
        out.extend_from_slice(&p.auth_header);
        out.extend_from_slice(b"\r\n");
    }
    out.extend_from_slice(b"User-Agent: portico-transport/0.1\r\nContent-Length: 0\r\n\r\n");
    out
}

/// What one INVITE needs.
#[derive(Debug, Clone, Default)]
pub struct InviteParams {
    pub host: String,
    pub local_addr: String,
    pub from_user: String,
    pub to_user: String,
    pub call_id: String,
    pub from_tag: String,
    pub cseq: i64,
    pub branch: String,
    pub sdp_body: String,
}

pub fn build_invite(p: &InviteParams) -> Vec<u8> {
    let aor_from = format!("sip:{}@{}", p.from_user, p.host);
    let aor_to = format!("sip:{}@{}", p.to_user, p.host);
    let mut b = String::new();
    let _ = write!(b, "INVITE sip:{}@{} SIP/2.0\r\n", p.to_user, p.host);
    let _ = write!(
        b,
        "Via: SIP/2.0/UDP {};branch={}\r\n",
        p.local_addr, p.branch
    );
    b.push_str("Max-Forwards: 70\r\n");
    let _ = write!(b, "From: <{}>;tag={}\r\n", aor_from, p.from_tag);
    let _ = write!(b, "To: <{aor_to}>\r\n");
    let _ = write!(b, "Call-ID: {}\r\n", p.call_id);
    let _ = write!(b, "CSeq: {} INVITE\r\n", p.cseq);
    let _ = write!(b, "Contact: <sip:{}@{}>\r\n", p.from_user, p.local_addr);
    b.push_str("Content-Type: application/sdp\r\n");
    let _ = write!(
        b,
        "Content-Length: {}\r\n\r\n{}",
        p.sdp_body.len(),
        p.sdp_body
    );
    b.into_bytes()
}

/// What an ACK or a BYE inside a dialog needs. The fields that come from the peer's headers
/// (`to_user`, `call_id`, `to_tag`, and for an answered call `from_user`) are bytes as received:
/// Go keeps them as raw-byte strings, so a Latin-1 or GBK byte in a Call-ID or tag goes back out
/// unchanged and the station can match the request to its dialog (R1-4).
#[derive(Debug, Clone, Default)]
pub struct DialogParams {
    pub host: String,
    pub local_addr: String,
    pub from_user: Vec<u8>,
    pub to_user: Vec<u8>,
    pub call_id: Vec<u8>,
    pub from_tag: Vec<u8>,
    pub to_tag: Vec<u8>,
    pub cseq: i64,
    pub branch: String,
}

/// Appends the pieces in order.
fn cat(b: &mut Vec<u8>, parts: &[&[u8]]) {
    for p in parts {
        b.extend_from_slice(p);
    }
}

fn dialog_request(
    method: &str,
    request_uri: &[u8],
    from: &[u8],
    to: &[u8],
    p: &DialogParams,
) -> Vec<u8> {
    let mut b = Vec::new();
    cat(
        &mut b,
        &[method.as_bytes(), b" ", request_uri, b" SIP/2.0\r\n"],
    );
    cat(
        &mut b,
        &[
            format!("Via: SIP/2.0/UDP {};branch={}\r\n", p.local_addr, p.branch).as_bytes(),
            b"Max-Forwards: 70\r\n",
            b"From: <",
            from,
            b">;tag=",
            &p.from_tag,
            b"\r\n",
            b"To: <",
            to,
            b">;tag=",
            &p.to_tag,
            b"\r\n",
            b"Call-ID: ",
            &p.call_id,
            b"\r\n",
            format!("CSeq: {} {method}\r\n", p.cseq).as_bytes(),
            b"Content-Length: 0\r\n\r\n",
        ],
    );
    b
}

/// `sip:<user>@<host>` with the user as bytes.
fn aor(user: &[u8], host: &str) -> Vec<u8> {
    let mut v = b"sip:".to_vec();
    cat(&mut v, &[user, b"@", host.as_bytes()]);
    v
}

/// The ACK that follows a 200 OK to our INVITE: the INVITE's own CSeq number, the remote tag.
pub fn build_ack(p: &DialogParams) -> Vec<u8> {
    let uri = aor(&p.to_user, &p.host);
    dialog_request("ACK", &uri, &aor(&p.from_user, &p.host), &uri, p)
}

/// A BYE ending an established dialog. `to_user` empty: the To is the bare host (UAC side);
/// set: the remote party's user (UAS side, RFC 3261 12.2.1.1).
pub fn build_bye(p: &DialogParams) -> Vec<u8> {
    let to = if p.to_user.is_empty() {
        format!("sip:{}", p.host).into_bytes()
    } else {
        aor(&p.to_user, &p.host)
    };
    dialog_request(
        "BYE",
        format!("sip:{}", p.host).as_bytes(),
        &aor(&p.from_user, &p.host),
        &to,
        p,
    )
}
