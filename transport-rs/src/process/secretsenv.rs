//! Port of `transport/cmd/portico-transport/secretsenv.go`: `-secrets-env <path>` (DEV-518).
//! Takes the SIP password, user, realm and the IPC token from a `KEY=VALUE` .env file (on
//! docker-host the box's `/home/user/.env`, mounted read-only) instead of 0400 secret files.
//!
//! Only four keys are ever looked at. The key is compared against the allow-list BEFORE its
//! value is touched, so `GATE_SIP_TOKEN` (the old app's owner token, which this process must
//! never hold) and every other key are skipped without being copied into any variable. Values
//! are never logged or put in errors. The file is read once, at start. Parsing follows
//! gate-sip's `loadEnvFile` (trim, skip blank / `#` / no-`=` lines, strip one pair of
//! surrounding quotes) plus an optional leading `export `. No shell is involved.
//!
//! The file is handled as BYTES, as Go does: a line that is not UTF-8 is read like any other
//! (only an allow-listed value is ever converted to text, lossily, as Go's JSON round trip would
//! turn it into U+FFFD), and `strings.TrimSpace`'s Unicode white space is reproduced on bytes.

use std::collections::HashMap;
use std::io::Read;
use std::sync::Arc;

use crate::ipc::{LineScanner, Scan, MIN_TOKEN_LEN};
use crate::standalone::Getenv;

/// `secretKeyToEnv`: the `.env` keys we read and the PORTICO variable each stands in for.
const SECRET_KEY_TO_ENV: [(&[u8], &str); 4] = [
    (b"GATE_SIP_PASSWORD", "PORTICO_SIP_PASSWORD"),
    (b"GATE_SIP_USER", "PORTICO_SIP_USER"),
    (b"GATE_SIP_REALM", "PORTICO_SIP_REALM"),
    (b"GATE_SIP_PEER_TOKEN", "PORTICO_IPC_TOKEN"),
];

/// The first char of `b` if it starts with a valid UTF-8 sequence, with its width.
fn first_char(b: &[u8]) -> Option<(char, usize)> {
    let head = &b[..b.len().min(4)];
    let valid = match std::str::from_utf8(head) {
        Ok(s) => s,
        Err(e) => std::str::from_utf8(&head[..e.valid_up_to()]).ok()?,
    };
    valid.chars().next().map(|c| (c, c.len_utf8()))
}

/// The last char of `b` if it ends with a valid UTF-8 sequence, with its width.
fn last_char(b: &[u8]) -> Option<(char, usize)> {
    for n in 1..=b.len().min(4) {
        if let Ok(s) = std::str::from_utf8(&b[b.len() - n..]) {
            let mut it = s.chars();
            if let (Some(c), None) = (it.next(), it.next()) {
                return Some((c, n));
            }
        }
    }
    None
}

/// `strings.TrimSpace` on bytes: Unicode white space at both ends; an invalid byte is not space.
fn trim_space(mut b: &[u8]) -> &[u8] {
    while let Some((c, w)) = first_char(b) {
        if !c.is_whitespace() {
            break;
        }
        b = &b[w..];
    }
    while let Some((c, w)) = last_char(b) {
        if !c.is_whitespace() {
            break;
        }
        b = &b[..b.len() - w];
    }
    b
}

fn lossy(b: &[u8]) -> String {
    String::from_utf8_lossy(b).into_owned()
}

/// `loadSecretsEnv`: PORTICO variable name to value, for the allow-listed keys present in the file.
pub fn load_secrets_env(path: &str) -> Result<HashMap<String, String>, String> {
    let mut f = crate::secretfile::open_secret_file(path, "the -secrets-env file")
        .map_err(|e| format!("-secrets-env: cannot open the file: {e}"))?;
    let read_err = |e: std::io::Error| {
        format!(
            "-secrets-env: reading the file: read {path}: {}",
            crate::gojson::io_err(&e)
        )
    };
    let mut total = 0usize;
    // The values stay bytes until the length checks have run, as in Go (`len(v)` is bytes).
    let mut raw: HashMap<&'static str, Vec<u8>> = HashMap::new();
    let mut lines = LineScanner::new();
    let mut chunk = vec![0u8; 64 * 1024];
    let mut eof = false;
    loop {
        // complete lines first, then more data
        loop {
            match lines.next_line() {
                Scan::Line(line) => take_line(&line, &mut raw),
                Scan::TooLong => {
                    return Err(
                        "-secrets-env: reading the file: bufio.Scanner: token too long".into(),
                    )
                }
                Scan::More => break,
            }
        }
        if eof {
            // a final line with no newline is still a token
            if let Some(line) = lines.finish() {
                take_line(&line, &mut raw);
            }
            break;
        }
        match f.read(&mut chunk) {
            Ok(0) => eof = true,
            Ok(n) => {
                total += n;
                if total > crate::secretfile::MAX_ENV_FILE {
                    return Err(format!(
                        "-secrets-env: reading the file: read {path}: the file is larger than {} bytes",
                        crate::secretfile::MAX_ENV_FILE
                    ));
                }
                lines.push(&chunk[..n])
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
            Err(e) => return Err(read_err(e)),
        }
    }
    if let Some(v) = raw.get("PORTICO_SIP_PASSWORD") {
        if v.len() < 8 || v.len() > 15 {
            return Err(format!(
                "-secrets-env: GATE_SIP_PASSWORD must be 8-15 characters (got {})",
                v.len()
            ));
        }
    }
    if let Some(v) = raw.get("PORTICO_IPC_TOKEN") {
        if v.len() < MIN_TOKEN_LEN {
            return Err(format!(
                "-secrets-env: GATE_SIP_PEER_TOKEN must be at least {MIN_TOKEN_LEN} characters (got {})",
                v.len()
            ));
        }
        if lossy(v).chars().any(char::is_whitespace) {
            return Err("-secrets-env: GATE_SIP_PEER_TOKEN must not contain whitespace".into());
        }
    }
    Ok(raw
        .into_iter()
        .map(|(k, v)| (k.to_string(), lossy(&v)))
        .collect())
}

/// One line of the file: records the value of an allow-listed key. The key is compared BEFORE
/// the value is looked at.
fn take_line(line: &[u8], out: &mut HashMap<&'static str, Vec<u8>>) {
    let line = trim_space(line);
    if line.is_empty() || line[0] == b'#' {
        return;
    }
    let Some(eq) = line.iter().position(|b| *b == b'=') else {
        return;
    };
    let mut key = trim_space(&line[..eq]);
    if let Some(rest) = key.strip_prefix(b"export") {
        if matches!(rest.first(), Some(b' ' | b'\t')) {
            key = trim_space(rest);
        }
    }
    let Some((_, name)) = SECRET_KEY_TO_ENV.iter().find(|(k, _)| *k == key) else {
        return; // never touch the value of any other key
    };
    let mut v = trim_space(&line[eq + 1..]);
    if v.len() >= 2
        && ((v[0] == b'"' && v[v.len() - 1] == b'"') || (v[0] == b'\'' && v[v.len() - 1] == b'\''))
    {
        v = &v[1..v.len() - 1];
    }
    out.insert(name, v.to_vec());
}

/// `withSecretsEnv`: a `getenv` that answers from the process environment first and falls back to
/// the file's values, so explicit `PORTICO_*` variables win. SIP user and realm default to
/// `8001` / `VDP` when neither source has them.
pub fn with_secrets_env(path: &str, getenv: Arc<Getenv>) -> Result<Arc<Getenv>, String> {
    let mut vals = load_secrets_env(path)?;
    vals.entry("PORTICO_SIP_USER".into())
        .or_insert_with(|| "8001".into());
    vals.entry("PORTICO_SIP_REALM".into())
        .or_insert_with(|| "VDP".into());
    Ok(Arc::new(move |k: &str| {
        let v = getenv(k);
        if !v.is_empty() {
            return v;
        }
        vals.get(k).cloned().unwrap_or_default()
    }))
}
