//! The URL scrubber (Go's `scrubLine`): a log line, a reason or a panic text never carries a URL,
//! because a URL can carry a credential or a token.

use crate::gostd::text::go_trim_space;

/// `scrubLine`: keeps an `ffmpeg` stderr line useful and URL-free (a URL can carry a
/// credential or a token): every `scheme://...` run becomes `<url>`, the result is trimmed and
/// capped at 300 bytes (Go cuts the string at byte 300, which can split a rune; the wire then
/// carries U+FFFD for the stump, as here).
pub fn scrub_line(l: &str) -> String {
    let t = go_trim_space(l);
    let b = t.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i].is_ascii_alphabetic() {
            let mut k = i + 1;
            while k < b.len()
                && (b[k].is_ascii_alphanumeric() || matches!(b[k], b'+' | b'.' | b'-'))
            {
                k += 1;
            }
            if b[k..].starts_with(b"://") {
                let mut e = k + 3;
                while e < b.len() && !matches!(b[e], b'\t' | b'\n' | 0x0c | b'\r' | b' ') {
                    e += 1;
                }
                if e > k + 3 {
                    out.extend_from_slice(b"<url>");
                    i = e;
                    continue;
                }
            }
            // no start inside this run can match either (they share its end): skip it whole
            out.extend_from_slice(&b[i..k]);
            i = k;
            continue;
        }
        out.push(b[i]);
        i += 1;
    }
    if out.len() > 300 {
        out.truncate(300);
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// A URL anywhere in `s` replaced, plus a secret (the API key) replaced by `***`: the scrub
/// every error and panic text of this family goes through.
pub fn scrub_all(s: &str, secret: &str) -> String {
    let mut t = s.to_string();
    if !secret.is_empty() {
        t = t.replace(secret, "***");
    }
    let l = scrub_line(&t);
    if secret.is_empty() {
        l
    } else {
        l.replace(secret, "***")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scrub_line_cuts_urls_and_length() {
        assert_eq!(
            scrub_line("  open rtsp://u:p@h/x failed; also http://a/b  "),
            "open <url> failed; also <url>"
        );
        assert_eq!(scrub_line(&"x".repeat(1000)).len(), 300);
    }

    #[test]
    fn scrub_line_edges() {
        for (i, o) in [
            ("", ""),
            ("a://", "a://"),
            ("a://b", "<url>"),
            ("xhttp://a", "<url>"),
            ("1http://a", "1<url>"),
            ("see ftp://h:1/ and rtsp://u:p@h x", "see <url> and <url> x"),
            ("://nope", "://nope"),
            ("proto+x.y-z://q r", "<url> r"),
            ("héllo http://é", "héllo <url>"),
        ] {
            assert_eq!(scrub_line(i), o, "{i:?}");
        }
        // a 300-byte cut that splits a rune comes out as the replacement character
        let s = format!("{}é", "x".repeat(299));
        assert_eq!(scrub_line(&s), format!("{}\u{fffd}", "x".repeat(299)));
    }

    #[test]
    fn scrub_all_removes_the_key_and_the_urls() {
        assert_eq!(
            scrub_all("fail KEY123 at rtsp://u:KEY123@h/x", "KEY123"),
            "fail *** at <url>"
        );
        assert_eq!(scrub_all("plain", ""), "plain");
    }
}
