//! Small helpers every part of the crate shares: the OS random source, hex, a poison-recovering
//! mutex lock, the text of a caught panic and Go's `base64.StdEncoding`. (Go's standard-library
//! look-alikes - `strconv`, `net`, `time`, `strings` - live in [`crate::gostd`].)

use std::sync::{Mutex, MutexGuard};

/// Fills `buf` from the OS random source (Go: `crypto/rand.Read`, which cannot fail in
/// practice; a failure here is fatal in the same spirit).
pub(crate) fn fill_random(buf: &mut [u8]) {
    getrandom::getrandom(buf).expect("OS random source unavailable");
}

/// `randHex(n)`: n random bytes as 2n lower-case hex digits.
pub(crate) fn rand_hex(n: usize) -> String {
    let mut b = vec![0u8; n];
    fill_random(&mut b);
    hex(&b)
}

pub(crate) fn rand_u32() -> u32 {
    let mut b = [0u8; 4];
    fill_random(&mut b);
    u32::from_be_bytes(b)
}

/// A uniform float in [0, 1), like `math/rand.Float64`.
pub(crate) fn rand_f64() -> f64 {
    let mut b = [0u8; 8];
    fill_random(&mut b);
    (u64::from_be_bytes(b) >> 11) as f64 / (1u64 << 53) as f64
}

pub(crate) fn hex(b: &[u8]) -> String {
    use std::fmt::Write;
    let mut s = String::with_capacity(b.len() * 2);
    for x in b {
        let _ = write!(s, "{x:02x}");
    }
    s
}

/// Locks `m`, ignoring poisoning (the crate's one policy, R1-27). Every critical section in this
/// crate leaves its data structurally valid at each point a panic can happen, and a panic in a
/// handler, a driver task or a read loop is contained (`catch_unwind` or a supervised task), so a
/// poisoned lock must not turn one contained panic into a panic in every later user of the data.
pub(crate) fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// The text of a panic payload (`&str` or `String`), or `fallback` for any other payload type.
pub(crate) fn panic_message(p: &(dyn std::any::Any + Send), fallback: &str) -> String {
    if let Some(s) = p.downcast_ref::<&str>() {
        (*s).to_string()
    } else if let Some(s) = p.downcast_ref::<String>() {
        s.clone()
    } else {
        fallback.to_string()
    }
}

/// [`panic_message`] of a joined task that panicked (`JoinError::is_panic`), scrubbed of every
/// registered secret and URL: the callers write it to the log themselves, past the panic hook
/// (DEV-796 D3).
pub(crate) fn join_panic_message(e: tokio::task::JoinError) -> String {
    crate::process::panic_hook::scrub(&panic_message(&*e.into_panic(), "non-string payload"))
}

#[cfg(test)]
mod join_panic_tests {
    #[tokio::test]
    async fn a_joined_panic_text_is_scrubbed_of_registered_secrets() {
        crate::process::panic_hook::register_secret("join-panic-secret-4417");
        let h = tokio::spawn(async { panic!("registrar failed for join-panic-secret-4417") });
        let e = h.await.unwrap_err();
        assert!(e.is_panic());
        let m = super::join_panic_message(e);
        assert!(!m.contains("join-panic-secret-4417"), "{m}");
        assert!(m.contains("***"), "{m}");
    }
}

// ---- base64 (Go's base64.StdEncoding) ----------------------------------------------------------

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// `base64.StdEncoding.EncodeToString`.
pub fn base64_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for c in data.chunks(3) {
        let n = (u32::from(c[0]) << 16)
            | (u32::from(*c.get(1).unwrap_or(&0)) << 8)
            | u32::from(*c.get(2).unwrap_or(&0));
        out.push(B64[(n >> 18) as usize & 63] as char);
        out.push(B64[(n >> 12) as usize & 63] as char);
        out.push(if c.len() > 1 {
            B64[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if c.len() > 2 {
            B64[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

/// `base64.StdEncoding.DecodeString` (padding required; CR and LF are skipped).
pub fn base64_decode(s: &str) -> Option<Vec<u8>> {
    let mut out = Vec::with_capacity(s.len() / 4 * 3);
    let mut acc: u32 = 0;
    let mut bits = 0;
    let mut pad = 0;
    let mut count = 0usize;
    for b in s.bytes() {
        if b == b'\r' || b == b'\n' {
            continue;
        }
        if b == b'=' {
            pad += 1;
            count += 1;
            continue;
        }
        if pad > 0 {
            return None;
        }
        let v = match b {
            b'A'..=b'Z' => b - b'A',
            b'a'..=b'z' => b - b'a' + 26,
            b'0'..=b'9' => b - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return None,
        } as u32;
        count += 1;
        acc = (acc << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    if count & 3 != 0 || pad > 2 {
        return None;
    }
    Some(out)
}

/// `ErrBadFrame` and `ErrClosed` of `wsmedia.Leg.Push`. It lives here, not in `wsmedia`, because
/// the house bridge's `HouseLeg` trait names it and the bridge core imports no transport (R2-50);
/// `wsmedia` re-exports it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PushError {
    BadFrame,
    Closed,
}

impl PushError {
    /// Go's `err.Error()`.
    pub fn message(&self) -> &'static str {
        match self {
            PushError::BadFrame => "wsmedia: frame has the wrong size for the codec",
            PushError::Closed => "wsmedia: leg is closed",
        }
    }
}
