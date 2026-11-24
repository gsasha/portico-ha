//! `strconv`: `Quote` (`%q`) and `Atoi`, with Go's error texts.

/// Whether Go's `unicode.IsPrint` would hold. Exact for ASCII and Latin-1; beyond that an
/// approximation (control, space separators, format characters, private use and noncharacters
/// are non-printable; unassigned code points are not detected).
fn is_print(c: char) -> bool {
    let u = c as u32;
    if u < 0x80 {
        return (0x20..0x7f).contains(&u);
    }
    if c.is_control() || c.is_whitespace() {
        return false;
    }
    !(u == 0xad
        || (0x600..=0x605).contains(&u)
        || u == 0x61c
        || u == 0x6dd
        || u == 0x70f
        || u == 0x180e
        || (0x200b..=0x200f).contains(&u)
        || (0x202a..=0x202e).contains(&u)
        || (0x2060..=0x2064).contains(&u)
        || (0x2066..=0x206f).contains(&u)
        || u == 0xfeff
        || (0xfff9..=0xfffb).contains(&u)
        || (0xe000..=0xf8ff).contains(&u)
        || (0xfdd0..=0xfdef).contains(&u)
        || u == 0x890
        || u == 0x891
        || u == 0x8e2
        || u == 0x110bd
        || u == 0x110cd
        || (0x13430..=0x1343f).contains(&u)
        || (0x1bca0..=0x1bca3).contains(&u)
        || (0x1d173..=0x1d17a).contains(&u)
        || u == 0xe0001
        || (0xe0020..=0xe007f).contains(&u)
        || (u & 0xfffe) == 0xfffe
        || u >= 0xf0000)
}

/// `strconv.Quote` (Go's `%q`).
pub fn go_quote(s: &str) -> String {
    let mut out = String::from("\"");
    quote_into(&mut out, s);
    out.push('"');
    out
}

/// `strconv.Quote` of a string that may not be UTF-8: each invalid byte is written `\xNN`, as Go
/// does (`width == 1 && r == RuneError`), where [`go_quote`] only sees text and so prints
/// U+FFFD raw. Use it for text that came off the network (R2-112).
pub fn go_quote_bytes(b: &[u8]) -> String {
    let mut out = String::from("\"");
    let mut rest = b;
    loop {
        match std::str::from_utf8(rest) {
            Ok(s) => {
                quote_into(&mut out, s);
                break;
            }
            Err(e) => {
                let (good, bad) = rest.split_at(e.valid_up_to());
                quote_into(&mut out, std::str::from_utf8(good).unwrap_or(""));
                // Go decodes one byte at a time: every byte of an ill-formed sequence is its own \x
                let n = e.error_len().unwrap_or(bad.len());
                for x in &bad[..n] {
                    out.push_str(&format!("\\x{x:02x}"));
                }
                rest = &bad[n..];
            }
        }
    }
    out.push('"');
    out
}

fn quote_into(out: &mut String, s: &str) {
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if is_print(c) => out.push(c),
            '\x07' => out.push_str("\\a"),
            '\x08' => out.push_str("\\b"),
            '\x0c' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\x0b' => out.push_str("\\v"),
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c if (c as u32) < 0x10000 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push_str(&format!("\\U{:08x}", c as u32)),
        }
    }
}

/// Why [`atoi`] refused a text, as `strconv.NumError.Err` (`ErrSyntax` / `ErrRange`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum AtoiError {
    Syntax,
    Range,
}

/// `strconv.Atoi` over bytes: the value or the reason. A range error is still a refusal here;
/// Go returns the clamp with it, which [`go_atoi_lenient`] keeps.
pub(crate) fn atoi(s: &[u8]) -> Result<i64, AtoiError> {
    let (neg, digits) = match s.first() {
        Some(b'+') => (false, &s[1..]),
        Some(b'-') => (true, &s[1..]),
        _ => (false, s),
    };
    if digits.is_empty() {
        return Err(AtoiError::Syntax);
    }
    let mut n: u64 = 0;
    for &c in digits {
        if !c.is_ascii_digit() {
            return Err(AtoiError::Syntax);
        }
        let d = u64::from(c - b'0');
        if n > u64::MAX / 10 {
            return Err(AtoiError::Range);
        }
        n *= 10;
        match n.checked_add(d) {
            Some(n1) => n = n1,
            None => return Err(AtoiError::Range),
        }
    }
    if !neg && n >= 1 << 63 {
        return Err(AtoiError::Range);
    }
    if neg && n > 1 << 63 {
        return Err(AtoiError::Range);
    }
    Ok(if neg {
        (n as i64).wrapping_neg()
    } else {
        n as i64
    })
}

/// `strconv.Atoi` over bytes with Go's error text (`strconv.Atoi: parsing "x": invalid syntax`);
/// an invalid UTF-8 byte in the text is quoted as `\xNN`, as Go's `%q` does (R2-112).
pub(crate) fn go_atoi_bytes(s: &[u8]) -> Result<i64, String> {
    atoi(s).map_err(|e| {
        let why = match e {
            AtoiError::Syntax => "invalid syntax",
            AtoiError::Range => "value out of range",
        };
        format!("strconv.Atoi: parsing {}: {why}", go_quote_bytes(s))
    })
}

/// `strconv.Atoi` with Go's error text (`strconv.Atoi: parsing "x": invalid syntax`).
pub(crate) fn go_atoi(s: &str) -> Result<i64, String> {
    go_atoi_bytes(s.as_bytes())
}

/// `n, _ := strconv.Atoi(s)`: the parsed value, 0 on a syntax error and the clamped
/// `MaxInt64` / `MinInt64` on a range error (Go returns the clamp together with the error).
pub(crate) fn go_atoi_lenient(s: &str) -> i64 {
    match atoi(s.as_bytes()) {
        Ok(n) => n,
        Err(AtoiError::Range) => {
            if s.starts_with('-') {
                i64::MIN
            } else {
                i64::MAX
            }
        }
        Err(AtoiError::Syntax) => 0,
    }
}

/// `strconv.ParseBool`.
pub(crate) fn parse_bool(v: &str) -> Option<bool> {
    match v {
        "1" | "t" | "T" | "true" | "TRUE" | "True" => Some(true),
        "0" | "f" | "F" | "false" | "FALSE" | "False" => Some(false),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn go_atoi_matches_strconv() {
        assert_eq!(go_atoi("42"), Ok(42));
        assert_eq!(go_atoi("+7"), Ok(7));
        assert_eq!(go_atoi("-9223372036854775808"), Ok(i64::MIN));
        assert_eq!(
            go_atoi("9223372036854775808").unwrap_err(),
            "strconv.Atoi: parsing \"9223372036854775808\": value out of range"
        );
        assert_eq!(
            go_atoi("1_0").unwrap_err(),
            "strconv.Atoi: parsing \"1_0\": invalid syntax"
        );
        assert!(go_atoi("").is_err());
        assert!(go_atoi("-").is_err());
        assert!(go_atoi(" 1").is_err());
    }

    // R1-76: `n, _ := strconv.Atoi(..)` keeps the clamped value of a range error.
    #[test]
    fn go_atoi_lenient_clamps_like_go() {
        assert_eq!(go_atoi_lenient("42"), 42);
        assert_eq!(go_atoi_lenient("x"), 0);
        assert_eq!(go_atoi_lenient("99999999999999999999"), i64::MAX);
        assert_eq!(go_atoi_lenient("-99999999999999999999"), i64::MIN);
    }

    // R2-88 / R2-61: the table Go's `strconv.Quote` gives (written from Go's rules: IsPrint is
    // false for control, Zs, Cf, Co and noncharacters; the \U form is for runes above U+FFFF).
    #[test]
    fn go_quote_table() {
        for (input, want) in [
            ("plain é ü", "\"plain é ü\""),
            ("\u{85}", "\"\\u0085\""),
            ("\u{a0}", "\"\\u00a0\""),
            ("\u{ad}", "\"\\u00ad\""),
            ("\u{e000}", "\"\\ue000\""),
            ("\u{200b}", "\"\\u200b\""),
            ("\u{feff}", "\"\\ufeff\""),
            ("\u{fffd}", "\"\u{fffd}\""),
            ("\u{1f600}", "\"\u{1f600}\""),
            ("\u{f0000}", "\"\\U000f0000\""),
            ("\u{10ffff}", "\"\\U0010ffff\""),
            ("\u{e0001}", "\"\\U000e0001\""),
            ("\u{e0041}", "\"\\U000e0041\""),
            ("\u{1d173}", "\"\\U0001d173\""),
            ("\u{13430}", "\"\\U00013430\""),
            ("\u{fdd0}", "\"\\ufdd0\""),
            ("\u{890}", "\"\\u0890\""),
            ("\u{110bd}", "\"\\U000110bd\""),
        ] {
            assert_eq!(go_quote(input), want, "{input:?}");
        }
    }

    // R2-112: an invalid byte is `\xNN`, one per byte, as Go's Quote writes it.
    #[test]
    fn go_quote_bytes_writes_invalid_bytes_as_hex() {
        assert_eq!(go_quote_bytes(b"re\xe9alm"), "\"re\\xe9alm\"");
        assert_eq!(go_quote_bytes(b"a\xe2\x82b"), "\"a\\xe2\\x82b\"");
        assert_eq!(go_quote_bytes(b"\xff"), "\"\\xff\"");
        assert_eq!(go_quote_bytes("é\u{fffd}".as_bytes()), "\"é\u{fffd}\"");
        assert_eq!(go_quote_bytes(b"x\xe2"), "\"x\\xe2\"");
        assert_eq!(go_quote_bytes(b""), "\"\"");
        assert_eq!(
            go_atoi_bytes(b"4\xff").unwrap_err(),
            "strconv.Atoi: parsing \"4\\xff\": invalid syntax"
        );
    }

    #[test]
    fn atoi_tells_a_range_error_from_a_syntax_error() {
        assert_eq!(atoi(b"9223372036854775808"), Err(AtoiError::Range));
        assert_eq!(atoi(b"-9223372036854775809"), Err(AtoiError::Range));
        assert_eq!(atoi(b"12x"), Err(AtoiError::Syntax));
        assert_eq!(atoi(b"-9223372036854775808"), Ok(i64::MIN));
    }

    // `%q` escapes what Go's `strconv.Quote` escapes (R1-55: gojson::quote was a weaker copy).
    #[test]
    fn go_quote_escapes_like_strconv_quote() {
        assert_eq!(go_quote("a\"b\\c"), "\"a\\\"b\\\\c\"");
        assert_eq!(
            go_quote("\x07\x08\x0c\n\r\t\x0b\x01\x7f"),
            "\"\\a\\b\\f\\n\\r\\t\\v\\x01\\x7f\""
        );
    }
}
