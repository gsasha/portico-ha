//! `strings` / `[]byte` to `string` look-alikes: Go's view of invalid UTF-8 and `strings.ToLower`.

/// Go's view of bytes as text for JSON and `%s`: every invalid UTF-8 byte becomes U+FFFD on its
/// own (`String::from_utf8_lossy` would fold a truncated sequence into one).
pub(crate) fn go_lossy(b: &[u8]) -> String {
    let mut out = String::with_capacity(b.len());
    let mut rest = b;
    loop {
        match std::str::from_utf8(rest) {
            Ok(s) => {
                out.push_str(s);
                return out;
            }
            Err(e) => {
                let (good, bad) = rest.split_at(e.valid_up_to());
                out.push_str(std::str::from_utf8(good).unwrap_or(""));
                out.push('\u{fffd}');
                rest = &bad[1..];
            }
        }
    }
}

/// `strings.Fields` over bytes: the runs between Unicode white space, each as the bytes received
/// (an invalid byte is not white space, as `utf8.RuneError` is not in Go).
pub(crate) fn go_fields_bytes(b: &[u8]) -> Vec<&[u8]> {
    let mut out = Vec::new();
    let (mut i, mut start) = (0, None);
    while i < b.len() {
        let w = match b[i] {
            0..=0x7f => 1,
            0xc0..=0xdf => 2,
            0xe0..=0xef => 3,
            0xf0..=0xf7 => 4,
            _ => 1,
        };
        let end = (i + w).min(b.len());
        let (space, n) = match std::str::from_utf8(&b[i..end]) {
            Ok(s) => (s.chars().next().is_some_and(char::is_whitespace), w),
            Err(_) => (false, 1),
        };
        match (space, start) {
            (true, Some(st)) => {
                out.push(&b[st..i]);
                start = None;
            }
            (false, None) => start = Some(i),
            _ => {}
        }
        i += n;
    }
    if let Some(st) = start {
        out.push(&b[st..]);
    }
    out
}

/// `strings.ToLower`: simple (one-to-one) Unicode lower-casing, per rune.
pub(crate) fn go_to_lower(s: &str) -> String {
    s.chars()
        .map(|c| {
            if c.is_ascii() {
                c.to_ascii_lowercase()
            } else {
                let mut it = c.to_lowercase();
                match (it.next(), it.next()) {
                    (Some(l), None) => l,
                    // U+0130 lowers to two runes in Rust; Go's simple mapping gives 'i'.
                    (Some(_), Some(_)) => 'i',
                    _ => c,
                }
            }
        })
        .collect()
}

/// `strings.TrimSpace`: Unicode white space at both ends. (`char::is_whitespace` is the Unicode
/// `White_Space` property, which already holds U+0085, U+00A0 and U+202F; U+2007 is added
/// defensively for Go's table, it is a no-op today.)
pub fn go_trim_space(s: &str) -> &str {
    s.trim_matches(|c: char| {
        c.is_whitespace() || c == '\u{85}' || c == '\u{a0}' || c == '\u{2007}' || c == '\u{202f}'
    })
}

/// The white space `encoding/json` skips: space, tab, CR, LF and nothing else (R2-8).
pub fn trim_json_space(s: &str) -> &str {
    s.trim_matches(|c| matches!(c, ' ' | '\t' | '\r' | '\n'))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_space_is_four_characters() {
        assert_eq!(trim_json_space(" \t\r\n{}\n"), "{}");
        for c in ['\u{c}', '\u{b}', '\u{a0}', '\u{85}', '\u{2028}'] {
            let s = format!("{c}{{}}{c}");
            assert_eq!(trim_json_space(&s), s, "{c:?}");
        }
    }

    #[test]
    fn lossy_replaces_each_bad_byte() {
        assert_eq!(go_lossy(&[b'a', 0xe2, 0x82, b'b']), "a\u{fffd}\u{fffd}b");
        assert_eq!(go_to_lower("ABC\u{130}"), "abci");
    }

    #[test]
    fn fields_over_bytes_match_the_text_fields() {
        let b = b"m=audio  4000\xff\t0\xc2\xa0 96 \xe2";
        let got = go_fields_bytes(b);
        assert_eq!(
            got,
            vec![&b"m=audio"[..], b"4000\xff", b"0", b"96", b"\xe2"]
        );
        let text = go_lossy(b);
        assert_eq!(text.split_whitespace().count(), got.len());
        assert!(go_fields_bytes(b"  ").is_empty());
    }
}
