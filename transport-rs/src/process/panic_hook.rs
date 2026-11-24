//! The process-wide panic hook (slice 7b review, PARITY.md 131): the default hook prints the
//! payload and the location of every panic to stderr BEFORE the unwind, so a `panic!` text that
//! carries an API key or an RTSP/RTP URL would reach the log whatever `catch_unwind` does after.
//! This hook prints a fixed `panic at <file>:<line>` and the payload scrubbed of every secret
//! registered by a driver at `configure` and of every `scheme://...` run. `panic = "unwind"` and
//! `catch_unwind` are untouched (the hook only reports).

use crate::util::lock;
use std::panic::PanicHookInfo;
use std::sync::Mutex;

static SECRETS: Mutex<Vec<String>> = Mutex::new(Vec::new());

/// How many secrets are kept; the oldest is forgotten for a newer one.
const MAX_SECRETS: usize = 64;

/// Registers a secret (an API key) to be scrubbed from every panic text. Idempotent. Past
/// [`MAX_SECRETS`] the OLDEST is dropped, not the newest refused: the key of a door configured
/// last is the one in use, and one of a door long deconfigured is not worth the room.
pub fn register_secret(s: &str) {
    let mut g = lock(&SECRETS);
    push_secret(&mut g, s);
}

fn push_secret(list: &mut Vec<String>, s: &str) {
    if s.len() < 4 {
        return; // a short string would blot out ordinary text
    }
    if list.iter().any(|x| x == s) {
        return;
    }
    if list.len() >= MAX_SECRETS {
        list.remove(0);
    }
    list.push(s.to_string());
}

/// `text` with every registered secret replaced by `***` and every URL by `<url>`.
pub fn scrub(text: &str) -> String {
    let secrets = lock(&SECRETS).clone();
    let mut t = text.to_string();
    for s in &secrets {
        t = t.replace(s.as_str(), "***");
    }
    let l = crate::process::scrub::scrub_line(&t);
    // scrub_line caps at 300 bytes; a panic text longer than that is cut, which is fine
    let mut l = l;
    for s in &secrets {
        l = l.replace(s.as_str(), "***");
    }
    l
}

fn payload_text(info: &PanicHookInfo<'_>) -> String {
    let p = info.payload();
    if let Some(s) = p.downcast_ref::<&str>() {
        s.to_string()
    } else if let Some(s) = p.downcast_ref::<String>() {
        s.clone()
    } else {
        "<non-string payload>".to_string()
    }
}

/// The report line of a panic: location fixed, payload scrubbed.
pub fn report(info: &PanicHookInfo<'_>) -> String {
    let loc = info
        .location()
        .map(|l| format!("{}:{}", l.file(), l.line()))
        .unwrap_or_else(|| "<unknown>".to_string());
    format!(
        "portico-transport: panic at {loc}: {}",
        scrub(&payload_text(info))
    )
}

/// A line on stdout that cannot panic when stdout is closed (`println!` can).
pub fn out_line(line: &str) {
    use std::io::Write;
    let _ = writeln!(std::io::stdout(), "{line}");
}

/// A line on stderr that cannot panic when stderr is closed (`eprintln!` can).
pub fn err_line(line: &str) {
    use std::io::Write;
    let _ = writeln!(std::io::stderr(), "{line}");
}

/// `log.Printf`'s default line prefix `YYYY/MM/DD HH:MM:SS ` for `secs` since the Unix epoch
/// (UTC here; Go uses the process's local zone).
pub fn utc_stamp(secs: u64) -> String {
    let (days, rem) = ((secs / 86_400) as i64, secs % 86_400);
    // civil-from-days (Howard Hinnant)
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!(
        "{y:04}/{m:02}/{d:02} {:02}:{:02}:{:02} ",
        rem / 3600,
        (rem / 60) % 60,
        rem % 60
    )
}

/// [`err_line`] with Go's `log.Printf` stamp in front (R2-145, D-20261114-1477). Only for lines
/// whose exact text no reader depends on: a line that Python matches by prefix (the house-call
/// summary), a start-up error the contract pins, or one a test compares stays on [`err_line`].
pub fn log_line(line: &str) {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    err_line(&format!("{}{line}", utc_stamp(now)));
}

/// Text on stderr with no newline added (the usage text), that cannot panic on a closed stderr.
pub fn err_text(text: &str) {
    use std::io::Write;
    let _ = write!(std::io::stderr(), "{text}");
}

/// Replaces the default hook. Call once, first thing in `main`.
pub fn install() {
    std::panic::set_hook(Box::new(|info| {
        use std::io::Write;
        // never panics when stderr is closed
        let _ = writeln!(std::io::stderr(), "{}", report(info));
    }));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_newest_secret_is_kept_when_the_list_is_full() {
        let mut l: Vec<String> = Vec::new();
        for i in 0..MAX_SECRETS + 6 {
            push_secret(&mut l, &format!("key-{i:04}"));
        }
        assert_eq!(l.len(), MAX_SECRETS);
        assert_eq!(l.last().unwrap(), &format!("key-{:04}", MAX_SECRETS + 5));
        assert!(!l.iter().any(|x| x == "key-0000"), "the oldest stayed");
        // idempotent, and a short one is ignored
        push_secret(&mut l, &format!("key-{:04}", MAX_SECRETS + 5));
        push_secret(&mut l, "ab");
        assert_eq!(l.len(), MAX_SECRETS);
    }

    #[test]
    fn the_stamp_is_go_log_printf_utc() {
        assert_eq!(utc_stamp(0), "1970/01/01 00:00:00 ");
        assert_eq!(utc_stamp(951_782_399), "2000/02/28 23:59:59 ");
        assert_eq!(utc_stamp(951_782_400), "2000/02/29 00:00:00 ");
        assert_eq!(utc_stamp(1_794_657_845), "2026/11/14 12:04:05 ");
        // a fixed-width prefix: 19 characters and one space
        assert_eq!(utc_stamp(1_700_000_000).len(), 20);
    }

    #[test]
    fn scrub_removes_registered_secrets_and_urls() {
        register_secret("KEY-ABCDEF");
        register_secret("ab"); // too short: ignored
        assert_eq!(
            scrub("boom KEY-ABCDEF at rtsp://u:KEY-ABCDEF@h/x ab"),
            "boom *** at <url> ab"
        );
    }
}
