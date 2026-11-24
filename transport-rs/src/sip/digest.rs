//! Port of `digest.go`, `digest_server.go`, `digest_client.go`: RFC 2617 MD5 digest, the client
//! side (challenge parsing, response, Authorization header), and the server side (nonce store,
//! verification of a REGISTER's Authorization).

use crate::util::lock;
use std::collections::HashMap;
use std::net::IpAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use md5::{Digest, Md5};

use super::message::trim_space_bytes;
use super::Error;
use crate::gostd::strconv::{go_quote as quote, go_quote_bytes};
use crate::gostd::text::{go_lossy, go_to_lower};
use crate::util::{hex, rand_hex};

fn md5hex(s: &[u8]) -> String {
    hex(&Md5::digest(s))
}

/// A parsed WWW-Authenticate / Proxy-Authenticate header (RFC 2617).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct DigestChallenge {
    pub realm: String,
    pub nonce: String,
    pub opaque: String,
    /// `""` (RFC 2069-style) or `"auth"`.
    pub qop: String,
    /// `"MD5"`, the only algorithm supported.
    pub algo: String,
}

/// [`DigestChallenge`] with the values as the bytes received. A realm or nonce with a Latin-1 byte
/// is hashed and echoed unrepaired, or the station (which hashes its own bytes) rejects the answer
/// (R1-5). Go does NOT do this: its `splitDigestFields` ranges over the string as runes and
/// `WriteRune` turns each invalid byte into U+FFFD (EF BF BD) before the hash and the echo. A
/// deliberate difference and a Go defect (PARITY 420, R2-4, `digest_raw.json`).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct RawChallenge {
    pub realm: Vec<u8>,
    pub nonce: Vec<u8>,
    pub opaque: Vec<u8>,
    /// `""` or `"auth"`.
    pub qop: Vec<u8>,
    pub algo: Vec<u8>,
}

impl RawChallenge {
    fn from_text(ch: &DigestChallenge) -> RawChallenge {
        RawChallenge {
            realm: ch.realm.clone().into_bytes(),
            nonce: ch.nonce.clone().into_bytes(),
            opaque: ch.opaque.clone().into_bytes(),
            qop: ch.qop.clone().into_bytes(),
            algo: ch.algo.clone().into_bytes(),
        }
    }
}

/// `a:b:c` joined as bytes.
fn colon(parts: &[&[u8]]) -> Vec<u8> {
    let mut v = Vec::new();
    for (i, p) in parts.iter().enumerate() {
        if i > 0 {
            v.push(b':');
        }
        v.extend_from_slice(p);
    }
    v
}

/// The RFC 2617 `response` over bytes. `cnonce` and `nc` only count when the challenge says
/// `qop=auth`.
pub fn digest_response_raw(
    username: &[u8],
    password: &[u8],
    method: &[u8],
    uri: &[u8],
    ch: &RawChallenge,
    cnonce: &[u8],
    nc: i64,
) -> String {
    let ha1 = md5hex(&colon(&[username, &ch.realm, password]));
    let ha2 = md5hex(&colon(&[method, uri]));
    if ch.qop == b"auth" {
        let nc = format!("{nc:08x}");
        return md5hex(&colon(&[
            ha1.as_bytes(),
            &ch.nonce,
            nc.as_bytes(),
            cnonce,
            &ch.qop,
            ha2.as_bytes(),
        ]));
    }
    md5hex(&colon(&[ha1.as_bytes(), &ch.nonce, ha2.as_bytes()]))
}

/// The RFC 2617 `response`. `cnonce` and `nc` only count when the challenge says `qop=auth`.
pub fn digest_response(
    username: &str,
    password: &str,
    method: &str,
    uri: &str,
    ch: &DigestChallenge,
    cnonce: &str,
    nc: i64,
) -> String {
    digest_response_raw(
        username.as_bytes(),
        password.as_bytes(),
        method.as_bytes(),
        uri.as_bytes(),
        &RawChallenge::from_text(ch),
        cnonce.as_bytes(),
        nc,
    )
}

/// The value of the Authorization header as bytes (note: `algorithm=MD5` is always written).
pub fn build_authorization_header_raw(
    username: &[u8],
    uri: &[u8],
    ch: &RawChallenge,
    response: &str,
    cnonce: &[u8],
    nc: i64,
) -> Vec<u8> {
    let mut b = b"Digest username=\"".to_vec();
    for part in [
        username,
        b"\", realm=\"",
        &ch.realm,
        b"\", nonce=\"",
        &ch.nonce,
        b"\", uri=\"",
        uri,
        b"\", response=\"",
        response.as_bytes(),
        b"\", algorithm=MD5",
    ] {
        b.extend_from_slice(part);
    }
    if ch.qop == b"auth" {
        b.extend_from_slice(format!(", qop=auth, nc={nc:08x}, cnonce=\"").as_bytes());
        b.extend_from_slice(cnonce);
        b.push(b'"');
    }
    if !ch.opaque.is_empty() {
        b.extend_from_slice(b", opaque=\"");
        b.extend_from_slice(&ch.opaque);
        b.push(b'"');
    }
    b
}

/// The value of the Authorization header (note: `algorithm=MD5` is always written).
pub fn build_authorization_header(
    username: &str,
    uri: &str,
    ch: &DigestChallenge,
    response: &str,
    cnonce: &str,
    nc: i64,
) -> String {
    let raw = build_authorization_header_raw(
        username.as_bytes(),
        uri.as_bytes(),
        &RawChallenge::from_text(ch),
        response,
        cnonce.as_bytes(),
        nc,
    );
    // every input was text, so the output is
    String::from_utf8(raw).unwrap_or_default()
}

/// Parses `Digest realm="...", nonce="...", qop="auth", opaque="...", algorithm=MD5` from the
/// header bytes as received. No backslash escapes inside values.
pub fn parse_digest_challenge_raw(header: &[u8]) -> Result<RawChallenge, Error> {
    let header_t = trim_space_bytes(header);
    let Some(rest) = header_t.strip_prefix(b"Digest ") else {
        return Err(Error::msg(format!(
            "sip: not a Digest challenge: {}",
            go_quote_bytes(header_t)
        )));
    };
    let mut ch = RawChallenge {
        algo: b"MD5".to_vec(),
        ..Default::default()
    };
    // Go iterates a map (random order); a later duplicate key replaces the earlier here, and the
    // fields are applied in first-appearance order, which makes the case-variant duplicates
    // ("Realm" and "realm") deterministic.
    for (k, v) in split_digest_fields(rest) {
        match go_to_lower(&go_lossy(trim_space_bytes(&k))).as_str() {
            "realm" => ch.realm = v,
            "nonce" => ch.nonce = v,
            "opaque" => ch.opaque = v,
            "qop" => {
                if v.windows(4).any(|w| w == b"auth") {
                    ch.qop = b"auth".to_vec();
                }
            }
            "algorithm" => ch.algo = v,
            _ => {}
        }
    }
    if ch.realm.is_empty() || ch.nonce.is_empty() {
        return Err(Error::msg(format!(
            "sip: incomplete Digest challenge: {}",
            go_quote_bytes(header_t)
        )));
    }
    Ok(ch)
}

/// Parses `Digest realm="...", nonce="...", qop="auth", opaque="...", algorithm=MD5`.
/// No backslash escapes inside values.
pub fn parse_digest_challenge(header: &str) -> Result<DigestChallenge, Error> {
    let raw = parse_digest_challenge_raw(header.as_bytes())?;
    // the input was text, so every field is
    let t = |v: Vec<u8>| String::from_utf8(v).unwrap_or_default();
    Ok(DigestChallenge {
        realm: t(raw.realm),
        nonce: t(raw.nonce),
        opaque: t(raw.opaque),
        qop: t(raw.qop),
        algo: t(raw.algo),
    })
}

type Field = (Vec<u8>, Vec<u8>);

/// Comma-separated `key=value` / `key="value"` list, byte by byte. The structure bytes (`"`, `=`
/// and `,`) are ASCII, so the split is Go's rune loop; what differs is the content: Go rebuilds
/// key and value with `WriteRune`, so an invalid UTF-8 byte becomes U+FFFD there, and here it
/// stays the byte received (PARITY 420). Returns the pairs with later duplicates folded into the
/// first one's slot.
fn split_digest_fields(s: &[u8]) -> Vec<Field> {
    let mut out: Vec<Field> = Vec::new();
    let (mut key, mut val) = (Vec::new(), Vec::new());
    let (mut in_quotes, mut in_key) = (false, true);
    fn flush(out: &mut Vec<Field>, key: &mut Vec<u8>, val: &mut Vec<u8>) {
        if !key.is_empty() {
            let k = trim_space_bytes(key).to_vec();
            match out.iter_mut().find(|(ek, _)| *ek == k) {
                Some(e) => e.1 = std::mem::take(val),
                None => out.push((k, std::mem::take(val))),
            }
        }
        key.clear();
        val.clear();
    }
    for &r in s {
        match r {
            b'"' => in_quotes = !in_quotes,
            b'=' if in_key && !in_quotes => in_key = false,
            b',' if !in_quotes => {
                flush(&mut out, &mut key, &mut val);
                in_key = true;
            }
            _ => {
                if in_key {
                    key.push(r)
                } else {
                    val.push(r)
                }
            }
        }
    }
    flush(&mut out, &mut key, &mut val);
    out
}

/// `AuthorizationFor` over bytes: answers a 401/407 challenge for one request and returns the
/// value of the Authorization / Proxy-Authorization header. MD5 only; `qop=auth` gets a fresh
/// cnonce, nc=1.
pub fn authorization_for_raw(
    challenge: &[u8],
    username: &[u8],
    password: &[u8],
    method: &[u8],
    uri: &[u8],
) -> Result<Vec<u8>, Error> {
    let ch = parse_digest_challenge_raw(challenge)?;
    let a = trim_space_bytes(&ch.algo);
    if !a.is_empty() && !a.eq_ignore_ascii_case(b"MD5") {
        return Err(Error::msg(format!(
            "sip: unsupported digest algorithm {}",
            quote(&go_lossy(a))
        )));
    }
    let cnonce = rand_hex(8);
    let resp = digest_response_raw(username, password, method, uri, &ch, cnonce.as_bytes(), 1);
    Ok(build_authorization_header_raw(
        username,
        uri,
        &ch,
        &resp,
        cnonce.as_bytes(),
        1,
    ))
}

/// `AuthorizationFor`: answers a 401/407 challenge for one request and returns the value of the
/// Authorization / Proxy-Authorization header. MD5 only; `qop=auth` gets a fresh cnonce, nc=1.
pub fn authorization_for(
    challenge: &str,
    username: &str,
    password: &str,
    method: &str,
    uri: &str,
) -> Result<String, Error> {
    let raw = authorization_for_raw(
        challenge.as_bytes(),
        username.as_bytes(),
        password.as_bytes(),
        method.as_bytes(),
        uri.as_bytes(),
    )?;
    Ok(String::from_utf8(raw).unwrap_or_default())
}

// ---- server side ------------------------------------------------------------------------------

/// How long an issued nonce stays acceptable.
pub const NONCE_TTL: Duration = Duration::from_secs(5 * 60);
/// Bound on the set; a flood of unauthenticated REGISTERs evicts the oldest instead of growing.
pub const NONCE_MAX: usize = 1024;

type Clock = Arc<dyn Fn() -> Instant + Send + Sync>;

#[derive(Default)]
struct Nonces {
    /// nonce -> (expiry, insertion order, the host it was issued to). The order breaks expiry
    /// ties deterministically (Go: whichever the map iteration meets first). The host is not in
    /// Go (PARITY 50): a nonce is good only for the host it was issued to.
    m: HashMap<String, (Instant, u64, Option<IpAddr>)>,
    seq: u64,
}

/// The nonces a server issued in its 401 challenges. A nonce is NOT single-use (no qop, so no nc).
#[derive(Default)]
pub struct NonceStore {
    clock: Option<Clock>,
    /// Milliseconds added to the store's own clock. It is read in EVERY build and written only by
    /// the `test-seams` setter, so the shipped and the tested store run the same code (R2-35).
    skew_ms: AtomicU64,
    inner: Mutex<Nonces>,
}

impl NonceStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// A store with an injected clock (the test seam of Go's `now` field).
    pub fn with_clock(clock: impl Fn() -> Instant + Send + Sync + 'static) -> Self {
        NonceStore {
            clock: Some(Arc::new(clock)),
            skew_ms: AtomicU64::new(0),
            inner: Mutex::default(),
        }
    }

    /// Test seam: moves the store's own clock forward ("a nonce that has expired").
    #[cfg(feature = "test-seams")]
    pub fn advance(&self, d: Duration) {
        self.skew_ms
            .fetch_add(d.as_millis() as u64, Ordering::SeqCst);
    }

    fn now(&self) -> Instant {
        match &self.clock {
            Some(c) => c(),
            None => Instant::now() + Duration::from_millis(self.skew_ms.load(Ordering::SeqCst)),
        }
    }

    /// A fresh random nonce, remembered for [`NONCE_TTL`].
    pub fn issue(&self) -> String {
        let nonce = rand_hex(16);
        self.insert(&nonce, None);
        nonce
    }

    /// [`Self::issue`] for a challenged host: the store remembers who the nonce was given to, with
    /// the same TTL and the same bound as the nonce itself (R1-171: one table, not two).
    pub fn issue_for(&self, host: IpAddr) -> String {
        let nonce = rand_hex(16);
        self.insert(&nonce, Some(host));
        nonce
    }

    /// The host a live nonce was issued to by [`Self::issue_for`].
    pub fn issued_to(&self, nonce: &str) -> Option<IpAddr> {
        let now = self.now();
        let g = lock(&self.inner);
        g.m.get(nonce)
            .filter(|(exp, _, _)| *exp > now)
            .and_then(|(_, _, host)| *host)
    }

    /// Remembers a given nonce as if issued now. Test and golden-vector seam: [`Self::issue`] is
    /// the API.
    #[cfg(feature = "test-seams")]
    pub fn insert_for_test(&self, nonce: &str) {
        self.insert(nonce, None);
    }

    fn insert(&self, nonce: &str, host: Option<IpAddr>) {
        let now = self.now();
        let mut g = lock(&self.inner);
        g.m.retain(|_, (exp, _, _)| *exp > now);
        while g.m.len() >= NONCE_MAX {
            // still full of live nonces: drop the soonest to expire
            let oldest =
                g.m.iter()
                    .min_by_key(|(_, (exp, seq, _))| (*exp, *seq))
                    .map(|(k, _)| k.clone());
            match oldest {
                Some(k) => {
                    g.m.remove(&k);
                }
                None => break,
            }
        }
        g.seq += 1;
        let seq = g.seq;
        g.m.insert(nonce.to_string(), (now + NONCE_TTL, seq, host));
    }

    /// Whether `nonce` was issued here and has not expired.
    pub fn valid(&self, nonce: &str) -> bool {
        let now = self.now();
        let mut g = lock(&self.inner);
        let Some((exp, _, _)) = g.m.get(nonce).copied() else {
            return false;
        };
        if exp <= now {
            g.m.remove(nonce);
            return false;
        }
        true
    }

    #[cfg(test)]
    pub(crate) fn len(&self) -> usize {
        lock(&self.inner).m.len()
    }
}

/// The outcome of [`verify_digest_authorization`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DigestResult {
    /// Response correct, nonce issued by us and fresh.
    Ok,
    /// Malformed, or the response does not match.
    Bad,
    /// Nonce unknown or expired: re-challenge, do not judge the password.
    Stale,
}

/// The pairs of Go's `(\w+)="([^"]*)"` over `s` (ASCII `\w`), leftmost non-overlapping. Over
/// bytes: Go's regexp reads an invalid byte as U+FFFD, which `[^"]` matches, and returns the
/// original bytes.
fn quoted_fields(s: &[u8]) -> Vec<Field> {
    let b = s;
    let is_w = |c: u8| c.is_ascii_alphanumeric() || c == b'_';
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if !is_w(b[i]) {
            i += 1;
            continue;
        }
        let mut j = i;
        while j < b.len() && is_w(b[j]) {
            j += 1;
        }
        if b[j..].starts_with(b"=\"") {
            if let Some(rel) = b[j + 2..].iter().position(|&c| c == b'"') {
                let k = j + 2 + rel;
                out.push((s[i..j].to_vec(), s[j + 2..k].to_vec()));
                i = k + 1;
                continue;
            }
        }
        // no match from i, nor from any start inside this word run
        i = j;
    }
    out
}

/// [`authorization_nonce`] over the header bytes.
pub fn authorization_nonce_raw(authorization: &[u8]) -> Vec<u8> {
    let mut nonce = Vec::new();
    for (k, v) in quoted_fields(authorization) {
        if k.eq_ignore_ascii_case(b"nonce") {
            nonce = v;
        }
    }
    nonce
}

/// The `nonce="..."` field of an Authorization header, as `VerifyDigestAuthorization` reads it
/// (case-insensitive key, last one wins); `""` when there is none. Not in Go: the server uses it
/// to bind a nonce to the host it was issued to.
pub fn authorization_nonce(authorization: &str) -> String {
    String::from_utf8(authorization_nonce_raw(authorization.as_bytes())).unwrap_or_default()
}

/// `VerifyDigestAuthorization` over the Authorization header as received: username and uri are
/// hashed as the bytes the station sent, like Go (R1-5). A nonce this store did not issue, or
/// that expired, is [`DigestResult::Stale`] before the password is looked at. `qop=auth` is not
/// understood.
pub fn verify_digest_authorization_raw(
    nonces: &NonceStore,
    authorization: &[u8],
    method: &str,
    realm: &str,
    password: &str,
) -> DigestResult {
    let mut f: HashMap<String, Vec<u8>> = HashMap::new();
    for (k, v) in quoted_fields(authorization) {
        // the key is `\w+`, so ASCII
        f.insert(go_to_lower(&go_lossy(&k)), v);
    }
    let get = |k: &str| f.get(k).map(Vec::as_slice).unwrap_or(b"");
    if get("username").is_empty()
        || get("uri").is_empty()
        || get("response").is_empty()
        || get("nonce").is_empty()
    {
        return DigestResult::Bad;
    }
    // a nonce we issued is hex, so one that is not UTF-8 was never issued
    let issued = std::str::from_utf8(get("nonce")).is_ok_and(|n| nonces.valid(n));
    if !issued {
        return DigestResult::Stale;
    }
    let ha1 = md5hex(&colon(&[
        get("username"),
        realm.as_bytes(),
        password.as_bytes(),
    ]));
    let ha2 = md5hex(&colon(&[method.as_bytes(), get("uri")]));
    if md5hex(&colon(&[ha1.as_bytes(), get("nonce"), ha2.as_bytes()])).as_bytes() != get("response")
    {
        return DigestResult::Bad;
    }
    DigestResult::Ok
}

/// `VerifyDigestAuthorization`: the RFC 2617 (no-qop, MD5) response of a REGISTER's Authorization
/// against the shared password and realm. A nonce this store did not issue, or that expired, is
/// [`DigestResult::Stale`] before the password is looked at. `qop=auth` is not understood.
pub fn verify_digest_authorization(
    nonces: &NonceStore,
    authorization: &str,
    method: &str,
    realm: &str,
    password: &str,
) -> DigestResult {
    verify_digest_authorization_raw(nonces, authorization.as_bytes(), method, realm, password)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    // Not in Go: the nonce field of an Authorization header, as the verifier reads it.
    #[test]
    fn authorization_nonce_is_read_like_the_verifier_reads_it() {
        assert_eq!(
            authorization_nonce(r#"Digest username="u", Nonce="abc", uri="x", nonce="def""#),
            "def"
        );
        assert_eq!(authorization_nonce(r#"Digest username="u""#), "");
        assert_eq!(authorization_nonce(""), "");
    }

    // TestDigestResponse
    #[test]
    fn digest_response_known_value() {
        let ch = DigestChallenge {
            realm: "test-realm".into(),
            nonce: "test-nonce-1234".into(),
            ..Default::default()
        };
        let got = digest_response("alice", "secret", "REGISTER", "sip:127.0.0.1", &ch, "", 0);
        assert_eq!(got, "254153973b80a878f650000d397f3bc0");
    }

    // TestParseDigestChallenge
    #[test]
    fn parse_digest_challenge_full() {
        let ch = parse_digest_challenge(
            r#"Digest realm="VTO", nonce="abc123", qop="auth", opaque="xyz", algorithm=MD5"#,
        )
        .unwrap();
        assert_eq!(
            (&*ch.realm, &*ch.nonce, &*ch.qop, &*ch.opaque),
            ("VTO", "abc123", "auth", "xyz")
        );
    }

    // TestParseDigestChallenge_NoQOP
    #[test]
    fn parse_digest_challenge_no_qop() {
        let ch = parse_digest_challenge(
            r#"Digest realm="test-realm", nonce="test-nonce-1234", algorithm=MD5"#,
        )
        .unwrap();
        assert_eq!(ch.realm, "test-realm");
        assert_eq!(ch.nonce, "test-nonce-1234");
        assert_eq!(ch.qop, "");
    }

    // TestAuthorizationForAgreesWithTheServerSideVerifier
    #[test]
    fn authorization_for_agrees_with_the_server_side_verifier() {
        let ns = NonceStore::new();
        let nonce = ns.issue();
        let chal = format!(r#"Digest realm="VDP", nonce="{nonce}", algorithm=MD5"#);
        let auth =
            authorization_for(&chal, "8001", "s3cretpass", "INVITE", "sip:9901@10.8.0.39").unwrap();
        assert_eq!(
            verify_digest_authorization(&ns, &auth, "INVITE", "VDP", "s3cretpass"),
            DigestResult::Ok,
            "{auth}"
        );
        assert_ne!(
            verify_digest_authorization(&ns, &auth, "INVITE", "VDP", "wrongpass1"),
            DigestResult::Ok
        );
        assert!(!auth.contains("s3cretpass"));
    }

    // TestAuthorizationForRefusesWhatItCannotAnswer
    #[test]
    fn authorization_for_refuses_what_it_cannot_answer() {
        for c in [
            r#"Basic realm="x""#,
            r#"Digest realm="VDP""#,
            r#"Digest realm="VDP", nonce="n", algorithm=SHA-256"#,
        ] {
            assert!(
                authorization_for(c, "u", "p", "INVITE", "sip:x").is_err(),
                "{c} was answered"
            );
        }
        let a = authorization_for(
            r#"Digest realm="VDP", nonce="n", qop="auth""#,
            "u",
            "p",
            "INVITE",
            "sip:x",
        )
        .unwrap();
        assert!(a.contains("qop=auth") && a.contains("cnonce="), "{a}");
    }

    const USER: &str = "8001";
    const PASS: &str = "s3cretpass";
    const URI: &str = "sip:127.0.0.1:5060";

    fn auth_for_nonce(nonce: &str, pass: &str) -> String {
        let ch = DigestChallenge {
            realm: "VDP".into(),
            nonce: nonce.into(),
            algo: "MD5".into(),
            ..Default::default()
        };
        build_authorization_header(
            USER,
            URI,
            &ch,
            &digest_response(USER, pass, "REGISTER", URI, &ch, "", 0),
            "",
            0,
        )
    }

    // TestVerifyDigestAuthorizationAgreesWithClientSide
    #[test]
    fn verify_agrees_with_client_side() {
        let ns = NonceStore::new();
        let nonce = ns.issue();
        let auth = auth_for_nonce(&nonce, PASS);
        assert_eq!(
            verify_digest_authorization(&ns, &auth, "REGISTER", "VDP", PASS),
            DigestResult::Ok,
            "{auth}"
        );
        let no_response =
            format!(r#"Digest username="8001", realm="VDP", nonce="{nonce}", uri="{URI}""#);
        let cases: [(&str, &str, &str, &str, &str); 5] = [
            ("wrong password", &auth, "REGISTER", "VDP", "otherpass1"),
            ("wrong realm", &auth, "REGISTER", "XXX", PASS),
            ("wrong method", &auth, "INVITE", "VDP", PASS),
            ("empty", "", "REGISTER", "VDP", PASS),
            ("no response", &no_response, "REGISTER", "VDP", PASS),
        ];
        for (name, a, method, realm, pass) in cases {
            assert_ne!(
                verify_digest_authorization(&ns, a, method, realm, pass),
                DigestResult::Ok,
                "{name}: accepted"
            );
        }
    }

    // TestVerifyDigestAuthorizationNonceLifecycle
    #[test]
    fn verify_nonce_lifecycle() {
        let base = Instant::now();
        let offset = Arc::new(AtomicU64::new(0)); // seconds
        let o2 = offset.clone();
        let ns =
            NonceStore::with_clock(move || base + Duration::from_secs(o2.load(Ordering::SeqCst)));
        let check = |nonce: &str| {
            verify_digest_authorization(&ns, &auth_for_nonce(nonce, PASS), "REGISTER", "VDP", PASS)
        };
        let fresh = ns.issue();
        let other = ns.issue();
        assert_eq!(check(&fresh), DigestResult::Ok);
        // Reuse within the TTL is fine.
        offset.store(NONCE_TTL.as_secs() - 1, Ordering::SeqCst);
        assert_eq!(check(&fresh), DigestResult::Ok);
        assert_eq!(check("neverissued0000"), DigestResult::Stale);
        // A wrong password on a live nonce is a mismatch, not a stale nonce.
        let bad = auth_for_nonce(&other, "otherpass1");
        assert_eq!(
            verify_digest_authorization(&ns, &bad, "REGISTER", "VDP", PASS),
            DigestResult::Bad
        );
        // Replayed after the TTL.
        offset.store(NONCE_TTL.as_secs() + 1, Ordering::SeqCst);
        assert_eq!(check(&fresh), DigestResult::Stale);
        assert_eq!(check(&fresh), DigestResult::Stale);
    }

    // TestNonceStoreIsBounded
    #[test]
    fn nonce_store_is_bounded() {
        let ns = NonceStore::new();
        let first = ns.issue();
        for _ in 0..NONCE_MAX + 10 {
            ns.issue();
        }
        assert!(ns.len() <= NONCE_MAX, "store holds {} nonces", ns.len());
        assert!(!ns.valid(&first), "the oldest nonce survived eviction");
    }

    #[test]
    fn quoted_fields_scan() {
        let f = quoted_fields(br#"Digest username="a b", x=1, y_2="", =""z="q""#);
        assert_eq!(
            f,
            vec![
                (b"username".to_vec(), b"a b".to_vec()),
                (b"y_2".to_vec(), Vec::new()),
                (b"z".to_vec(), b"q".to_vec()),
            ]
        );
    }

    // R1-5: realm/nonce are hashed and echoed as received. Expected hashes computed with
    // Python's hashlib over the raw bytes (not by this code).
    #[test]
    fn challenge_with_non_utf8_bytes_is_hashed_and_echoed_raw() {
        let hdr = b"Digest realm=\"re\xe9alm\", nonce=\"n\xffn1\", algorithm=MD5";
        let ch = parse_digest_challenge_raw(hdr).unwrap();
        assert_eq!(ch.realm, b"re\xe9alm");
        assert_eq!(ch.nonce, b"n\xffn1");
        let r = digest_response_raw(
            b"alice",
            b"secret",
            b"REGISTER",
            b"sip:127.0.0.1",
            &ch,
            b"",
            0,
        );
        assert_eq!(r, "740147e1c1b4555559a6b48ccf90cb99");
        let auth =
            authorization_for_raw(hdr, b"alice", b"secret", b"REGISTER", b"sip:127.0.0.1").unwrap();
        let has = |n: &[u8]| auth.windows(n.len()).any(|w| w == n);
        assert!(
            has(b"realm=\"re\xe9alm\"")
                && has(b"nonce=\"n\xffn1\"")
                && has(b"response=\"740147e1c1b4555559a6b48ccf90cb99\"")
        );
    }

    #[test]
    fn verify_hashes_a_raw_username() {
        let ns = NonceStore::new();
        ns.insert_for_test("abc123");
        let a = b"Digest username=\"al\xe9ce\", realm=\"VDP\", nonce=\"abc123\", uri=\"sip:127.0.0.1\", response=\"254afbe85b4217d7073967ceace0f69a\"";
        assert_eq!(
            verify_digest_authorization_raw(&ns, a, "REGISTER", "VDP", "s3cretpass"),
            DigestResult::Ok
        );
        assert_eq!(authorization_nonce_raw(a), b"abc123");
        // a non-UTF-8 nonce was never issued
        let b = b"Digest username=\"u\", nonce=\"a\xff\", uri=\"x\", response=\"r\"";
        assert_eq!(
            verify_digest_authorization_raw(&ns, b, "REGISTER", "VDP", "p"),
            DigestResult::Stale
        );
    }
}
