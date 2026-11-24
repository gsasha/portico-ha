//! Go `encoding/json` look-alikes: the parts of its behaviour that reach the wire.
//!
//! Python sees Go's `json.Unmarshal` error text inside `bad_params` messages and Go's
//! `Encoder` string escaping in every line, so both are reproduced here for the shapes the
//! request handlers use (string, int, float64 and `map[string]any` fields).

use serde_json::Value;
use std::collections::BTreeMap;

/// The Go field types the handlers decode into.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    Str,
    Int,
    Float,
    Map,
}

/// One struct field: its JSON name (the `json:"..."` tag) and Go type.
pub struct Spec {
    pub json: &'static str,
    pub kind: Kind,
}

impl Kind {
    fn go_type(self) -> &'static str {
        match self {
            Kind::Str => "string",
            Kind::Int => "int",
            Kind::Float => "float64",
            Kind::Map => "map[string]interface {}",
        }
    }
}

/// JSON kind name as Go's `UnmarshalTypeError.Value` spells it.
fn value_kind(raw: &str) -> &'static str {
    match raw.as_bytes().first() {
        Some(b'"') => "string",
        Some(b'{') => "object",
        Some(b'[') => "array",
        Some(b't') | Some(b'f') => "bool",
        _ => "number",
    }
}

/// Decode `raw` (a request's `params`, `""` when the field was absent) into the fields of Go
/// struct `struct_name`, as `json.Unmarshal(raw, &s)` would, returning one slot per spec
/// (`None` = left at its zero value). The error is Go's `err.Error()` text.
pub fn decode(raw: &str, struct_name: &str, specs: &[Spec]) -> Result<Vec<Option<Value>>, String> {
    match decode_lenient(raw, struct_name, specs) {
        (out, None) => Ok(out),
        (_, Some(e)) => Err(e),
    }
}

/// [`decode`] with Go's actual contract: the slots that could be decoded together with the first
/// error. `json.Unmarshal` skips a field of the wrong type, goes on with the others and returns
/// the first `UnmarshalTypeError` at the end, so `_ = json.Unmarshal(raw, &p)` keeps what
/// decoded. A syntax error (or a `params` that is not an object) leaves every slot unset, as Go
/// validates the whole text before it stores anything.
pub fn decode_lenient(
    raw: &str,
    struct_name: &str,
    specs: &[Spec],
) -> (Vec<Option<Value>>, Option<String>) {
    let mut out: Vec<Option<Value>> = vec![None; specs.len()];
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return (out, Some("unexpected end of JSON input".into()));
    }
    if trimmed == "null" {
        return (out, None);
    }
    if !trimmed.starts_with('{') {
        return (
            out,
            Some(format!(
                "json: cannot unmarshal {} into Go value of type main.{}",
                value_kind(trimmed),
                struct_name
            )),
        );
    }
    let entries: Vec<(String, Box<serde_json::value::RawValue>)> =
        match crate::ipc::parse_object_entries(trimmed.as_bytes()) {
            Some(e) => e,
            None => return (out, Some("unexpected end of JSON input".into())),
        };
    let mut first_err: Option<String> = None;
    for (key, val) in entries {
        let idx = specs
            .iter()
            .position(|s| s.json == key)
            .or_else(|| specs.iter().position(|s| equal_fold(s.json, &key)));
        let Some(i) = idx else { continue };
        let spec = &specs[i];
        let text = val.get();
        if text == "null" {
            // Go: `null` leaves a string/number alone and sets a map to nil (R2-63)
            if spec.kind == Kind::Map {
                out[i] = None;
            }
            continue;
        }
        let field_err = |value: String| {
            format!(
                "json: cannot unmarshal {} into Go struct field {}.{} of type {}",
                value,
                struct_name,
                spec.json,
                spec.kind.go_type()
            )
        };
        let kind = value_kind(text);
        let decoded: Result<Value, String> = match spec.kind {
            Kind::Str => {
                if kind == "string" {
                    Ok(Value::String(unquote(text)))
                } else {
                    Err(field_err(kind.to_string()))
                }
            }
            Kind::Int => {
                if kind == "number" {
                    match text.parse::<i64>() {
                        Ok(n) => Ok(Value::from(n)),
                        Err(_) => Err(field_err(format!("number {text}"))),
                    }
                } else {
                    Err(field_err(kind.to_string()))
                }
            }
            Kind::Float => {
                if kind == "number" {
                    match text.parse::<f64>() {
                        Ok(f) if f.is_finite() => Ok(serde_json::Number::from_f64(f)
                            .map(Value::Number)
                            .unwrap_or(Value::Null)),
                        _ => Err(field_err(format!("number {text}"))),
                    }
                } else {
                    Err(field_err(kind.to_string()))
                }
            }
            Kind::Map => {
                if kind == "object" {
                    // R1-3: a value `serde_json` cannot hold (1e999, nesting past its limit)
                    // is Go's Unmarshal error (`bad_params`), never a silently empty map.
                    map_value(text).map_err(|bad| match bad {
                        // Go names the type of the NUMBER's destination (an `any` holds a float64), not the map
                        MapErr::Number(lit) => format!(
                            "json: cannot unmarshal number {lit} into Go struct field {}.{} of type float64",
                            struct_name, spec.json
                        ),
                        MapErr::TooDeep => "exceeded max depth".to_string(),
                    })
                } else {
                    Err(field_err(kind.to_string()))
                }
            }
        };
        match decoded {
            // a second object for a map field is decoded INTO the existing map: the keys of the
            // first stay, a repeated key takes the later value (R2-63)
            Ok(Value::Object(new)) if spec.kind == Kind::Map => match out[i].as_mut() {
                Some(Value::Object(old)) => old.extend(new),
                _ => out[i] = Some(Value::Object(new)),
            },
            Ok(v) => out[i] = Some(v),
            Err(e) => {
                if first_err.is_none() {
                    first_err = Some(e);
                }
            }
        }
    }
    (out, first_err)
}

/// Go's `bytes.EqualFold` for a JSON key: Unicode simple folding, so U+017F (long s) matches `s`
/// and U+212A (Kelvin sign) matches `k` (R2-62). Upper-casing then lower-casing one character
/// walks the fold orbit of every character that has a single-character mapping.
fn equal_fold(a: &str, b: &str) -> bool {
    fn fold(c: char) -> char {
        if c == '\u{131}' {
            return c; // dotless i: upper-cases to I but is not in i's fold orbit (Turkic-only mapping)
        }
        fn one(mut it: impl Iterator<Item = char>, d: char) -> char {
            match (it.next(), it.next()) {
                (Some(x), None) => x,
                _ => d,
            }
        }
        let u = one(c.to_uppercase(), c);
        one(u.to_lowercase(), u)
    }
    a.chars().map(fold).eq(b.chars().map(fold))
}

/// A `map[string]interface{}` field's value, decoded as Go's `encoding/json` decodes it: a lone
/// surrogate escape in a string becomes U+FFFD (as in [`unquote`]) where `serde_json` refuses the
/// document, and a number that does not fit a `float64` is an error carrying its literal.
/// Nesting deeper than `serde_json`'s limit of 128 is also refused (PARITY: Go allows 10 000).
fn map_value(text: &str) -> Result<Value, MapErr> {
    fn go(text: &str, depth: usize) -> Result<Value, MapErr> {
        if depth > 128 {
            return Err(MapErr::TooDeep);
        }
        match text.as_bytes().first() {
            Some(b'"') => Ok(Value::String(unquote(text))),
            Some(b'{') => {
                let entries =
                    crate::ipc::parse_object_entries(text.as_bytes()).ok_or(MapErr::TooDeep)?;
                let mut m = serde_json::Map::new();
                for (k, v) in entries {
                    m.insert(k, go(v.get(), depth + 1)?); // the last duplicate wins, as in Go
                }
                Ok(Value::Object(m))
            }
            Some(b'[') => {
                let items: Vec<Box<serde_json::value::RawValue>> =
                    serde_json::from_str(text).map_err(|_| MapErr::TooDeep)?;
                items
                    .iter()
                    .map(|v| go(v.get(), depth + 1))
                    .collect::<Result<Vec<_>, _>>()
                    .map(Value::Array)
            }
            _ => serde_json::from_str::<Value>(text).map_err(|_| MapErr::Number(text.to_string())),
        }
    }
    go(text, 0)
}

enum MapErr {
    /// a number literal `serde_json` cannot hold (Go: "cannot unmarshal number 1e999 into ...")
    Number(String),
    /// nesting past 128 levels
    TooDeep,
}

/// The value of a JSON string literal (`text` includes the quotes and is syntactically valid) as
/// Go's `encoding/json` reads it: a lone surrogate escape (`\ud800`, a high one not followed by a
/// low one, or a low one on its own) becomes U+FFFD, where `serde_json` refuses the whole string.
pub(crate) fn unquote(text: &str) -> String {
    if let Ok(s) = serde_json::from_str::<String>(text) {
        return s;
    }
    let inner = text
        .strip_prefix('"')
        .and_then(|t| t.strip_suffix('"'))
        .unwrap_or(text);
    let b: Vec<char> = inner.chars().collect();
    let hex4 = |at: usize| -> Option<u32> {
        let h: String = b.get(at..at + 4)?.iter().collect();
        u32::from_str_radix(&h, 16).ok()
    };
    let mut out = String::with_capacity(inner.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] != '\\' {
            out.push(b[i]);
            i += 1;
            continue;
        }
        i += 1;
        let Some(&e) = b.get(i) else { break };
        i += 1;
        match e {
            'b' => out.push('\u{8}'),
            'f' => out.push('\u{c}'),
            'n' => out.push('\n'),
            'r' => out.push('\r'),
            't' => out.push('\t'),
            'u' => {
                let Some(hi) = hex4(i) else {
                    out.push('\u{fffd}');
                    continue;
                };
                i += 4;
                match hi {
                    0xD800..=0xDBFF => {
                        let low = (b.get(i) == Some(&'\\') && b.get(i + 1) == Some(&'u'))
                            .then(|| hex4(i + 2))
                            .flatten()
                            .filter(|l| (0xDC00..=0xDFFF).contains(l));
                        match low {
                            Some(l) => {
                                i += 6;
                                out.push(
                                    char::from_u32(0x10000 + ((hi - 0xD800) << 10) + (l - 0xDC00))
                                        .unwrap_or('\u{fffd}'),
                                );
                            }
                            None => out.push('\u{fffd}'),
                        }
                    }
                    0xDC00..=0xDFFF => out.push('\u{fffd}'),
                    c => out.push(char::from_u32(c).unwrap_or('\u{fffd}')),
                }
            }
            c => out.push(c), // `"` `\\` `/`
        }
    }
    out
}

/// String field `i` of a decoded struct (`""` when unset).
pub fn str_at(vals: &[Option<Value>], i: usize) -> String {
    match vals.get(i) {
        Some(Some(Value::String(s))) => s.clone(),
        _ => String::new(),
    }
}

/// Float field `i` of a decoded struct (`0.0` when unset).
pub fn f64_at(vals: &[Option<Value>], i: usize) -> f64 {
    match vals.get(i) {
        Some(Some(v)) => v.as_f64().unwrap_or(0.0),
        _ => 0.0,
    }
}

/// Append `s` as a JSON string the way Go's `json.Marshal` does with HTML escaping on
/// (`<`, `>`, `&`, U+2028, U+2029 as `\u00xx`/`\u202x`; `\b \f \n \r \t` short forms).
pub fn push_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '<' | '>' | '&' | '\u{2028}' | '\u{2029}' => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

/// A member value for [`object`].
#[derive(Clone, Copy)]
pub enum Field<'a> {
    Str(&'a str),
    Bool(bool),
    /// JSON text that is already marshalled (a nested object)
    Raw(&'a str),
}

/// A JSON object with its members in the order given, as Go marshals a struct (a `Value::Object`
/// sorts its keys). Strings are escaped as [`push_string`] does.
pub fn object(fields: &[(&str, Field)]) -> String {
    let mut out = String::from("{");
    for (i, (k, v)) in fields.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        push_string(&mut out, k);
        out.push(':');
        match v {
            Field::Str(s) => push_string(&mut out, s),
            Field::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Field::Raw(r) => out.push_str(r),
        }
    }
    out.push('}');
    out
}

/// `json.Marshal` of a value: compact, map keys sorted, Go string escaping.
pub fn marshal(v: &Value) -> String {
    let mut s = String::new();
    write_value(&mut s, v);
    s
}

fn write_value(out: &mut String, v: &Value) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => match n.as_f64().filter(|_| n.is_f64()) {
            Some(f) => out.push_str(&go_float(f)),
            None => out.push_str(&n.to_string()),
        },
        Value::String(s) => push_string(out, s),
        Value::Array(a) => {
            out.push('[');
            for (i, e) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(out, e);
            }
            out.push(']');
        }
        Value::Object(m) => {
            // serde_json's default map is a BTreeMap, so iteration is key-sorted like Go's.
            let sorted: BTreeMap<&String, &Value> = m.iter().collect();
            out.push('{');
            for (i, (k, e)) in sorted.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                push_string(out, k);
                out.push(':');
                write_value(out, e);
            }
            out.push('}');
        }
    }
}

/// `strconv.AppendFloat(b, f, 'f', -1, 64)` as `encoding/json` uses it: the shortest digits that
/// round-trip, without an exponent between 1e-6 and 1e21 (`5060`, not `5060.0`; the digits of
/// 2^63 are `9223372036854775808` in exact arithmetic and `9223372036854776000` here, as Go prints
/// them), otherwise `1.2e+21` / `1.2e-7`.
pub fn go_float(f: f64) -> String {
    if f == 0.0 {
        return if f.is_sign_negative() {
            "-0".into()
        } else {
            "0".into()
        };
    }
    let abs = f.abs();
    let sci = format!("{abs:e}"); // shortest round-trip digits: "9.223372036854776e18"
    let (mant, exp) = sci.split_once('e').expect("exponent");
    let exp: i32 = exp.parse().expect("exponent number");
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let sign = if f < 0.0 { "-" } else { "" };
    if abs < 1e-6 || abs >= 1e21 {
        let m = if digits.len() > 1 {
            format!("{}.{}", &digits[..1], &digits[1..])
        } else {
            digits
        };
        let e = if exp < 0 {
            format!("-{}", -exp)
        } else {
            format!("+{exp}")
        };
        return format!("{sign}{m}e{e}");
    }
    let body = if exp >= 0 {
        let int_len = exp as usize + 1;
        if digits.len() <= int_len {
            format!("{digits}{}", "0".repeat(int_len - digits.len()))
        } else {
            format!("{}.{}", &digits[..int_len], &digits[int_len..])
        }
    } else {
        format!("0.{}{digits}", "0".repeat((-exp - 1) as usize))
    };
    format!("{sign}{body}")
}

/// Go-style spelling of an OS error: lower-case, without Rust's " (os error N)" suffix.
pub fn io_err(e: &std::io::Error) -> String {
    let s = e.to_string();
    let s = match s.find(" (os error") {
        Some(i) => &s[..i],
        None => &s,
    };
    let mut chars = s.chars();
    match chars.next() {
        Some(c) => c.to_lowercase().collect::<String>() + chars.as_str(),
        None => String::new(),
    }
}

#[cfg(test)]
mod map_tests {
    use super::{decode, Kind, Spec};
    use serde_json::json;

    const S: [Spec; 1] = [Spec {
        json: "config",
        kind: Kind::Map,
    }];

    // R1-3. Texts checked against the real Go decoder (`json.Unmarshal` into
    // `struct{Config map[string]any}`), see the Phase R round-1 log.
    #[test]
    fn a_map_value_serde_cannot_hold_is_go_s_error_not_an_empty_map() {
        let e = decode(r#"{"config":{"a":1e999}}"#, "configureParams", &S).unwrap_err();
        assert_eq!(
            e,
            "json: cannot unmarshal number 1e999 into Go struct field configureParams.config of type float64"
        );
        let e = decode(r#"{"config":{"a":{"b":[1e999]}}}"#, "configureParams", &S).unwrap_err();
        assert!(e.contains("number 1e999"), "{e}");
    }

    // R2-63. Go: the second object is decoded INTO the existing map (top-level keys merge, a
    // nested value is replaced), and a later `null` sets the map to nil. From the encoding/json
    // documentation ("To unmarshal JSON into a map ... reuses the existing map, keeping existing
    // entries"; "null ... sets a map to nil"); not executed in Go.
    #[test]
    fn a_repeated_map_field_merges_and_null_resets_it() {
        let v = decode(
            r#"{"config":{"a":1,"n":{"x":1}},"config":{"b":2,"n":{"y":2}}}"#,
            "configureParams",
            &S,
        )
        .unwrap();
        assert_eq!(v[0], Some(json!({"a":1,"b":2,"n":{"y":2}})));
        let v = decode(r#"{"config":{"a":1},"config":null}"#, "configureParams", &S).unwrap();
        assert_eq!(v[0], None);
        let v = decode(r#"{"config":null,"config":{"a":1}}"#, "configureParams", &S).unwrap();
        assert_eq!(v[0], Some(json!({"a":1})));
    }

    // R2-62: Unicode simple folding of the key (bytes.EqualFold), not only ASCII.
    #[test]
    fn keys_fold_like_bytes_equalfold() {
        let s = [
            Spec {
                json: "reason",
                kind: Kind::Str,
            },
            Spec {
                json: "kind",
                kind: Kind::Str,
            },
        ];
        let v = decode("{\"rea\u{17f}on\":\"x\",\"\u{212a}IND\":\"y\"}", "p", &s).unwrap();
        assert_eq!(v, vec![Some(json!("x")), Some(json!("y"))]);
        let v = decode(r#"{"REASON":"x","kınd":"z"}"#, "p", &s).unwrap(); // dotless i does not fold to i
        assert_eq!(v, vec![Some(json!("x")), None]);
    }

    #[test]
    fn a_lone_surrogate_in_a_map_value_is_u_fffd() {
        let v = decode(
            r#"{"config":{"a":"x\ud800y","b":["\udc00"],"c":{"d":"\ud800"}}}"#,
            "configureParams",
            &S,
        )
        .unwrap();
        assert_eq!(
            v[0],
            Some(json!({"a":"x\u{fffd}y","b":["\u{fffd}"],"c":{"d":"\u{fffd}"}}))
        );
    }

    #[test]
    fn ordinary_maps_are_unchanged() {
        let v = decode(
            r#"{"config":{"n":5060,"f":1.5,"t":true,"z":null,"s":"é","l":[1,"a"],"o":{"k":1},"n":7}}"#,
            "configureParams",
            &S,
        )
        .unwrap();
        assert_eq!(
            v[0],
            Some(json!({"n":7,"f":1.5,"t":true,"z":null,"s":"é","l":[1,"a"],"o":{"k":1}}))
        );
    }
}

#[cfg(test)]
mod unquote_tests {
    use super::unquote;

    // Go's encoding/json: a lone surrogate escape is U+FFFD, a pair is one rune, the rest of the
    // string is untouched (the expectations are what the real Go decoder returned for these
    // inputs in tests/golden/wsmedia.json and by hand).
    #[test]
    fn lone_surrogates_become_the_replacement_character() {
        assert_eq!(unquote(r#""\ud800""#), "\u{fffd}");
        assert_eq!(unquote(r#""\udc00""#), "\u{fffd}");
        assert_eq!(unquote(r#""a\ud800b""#), "a\u{fffd}b");
        assert_eq!(unquote(r#""\ud800A""#), "\u{fffd}A");
        assert_eq!(unquote(r#""\ud800\ud800""#), "\u{fffd}\u{fffd}");
        assert_eq!(unquote(r#""😀""#), "\u{1f600}");
        assert_eq!(unquote(r#""😀\ud83d""#), "\u{1f600}\u{fffd}");
        assert_eq!(unquote(r#""x\n\"\\\/\té""#), "x\n\"\\/\té");
    }
}

#[cfg(test)]
mod float_tests {
    use super::go_float;

    // What Go's encoding/json prints for a float64 (checked against the real encoder in
    // tests/golden/standalone.json for 0 and i64::MAX, and by hand for the rest).
    #[test]
    fn floats_are_spelled_as_go_spells_them() {
        for (f, want) in [
            (0.0, "0"),
            (-0.0, "-0"),
            (5060.0, "5060"),
            (-1.0, "-1"),
            (0.5, "0.5"),
            (0.1, "0.1"),
            (123456.789, "123456.789"),
            (9223372036854775807.0, "9223372036854776000"),
            (1e20, "100000000000000000000"),
            (1e21, "1e+21"),
            (1.5e300, "1.5e+300"),
            (0.000001, "0.000001"),
            (0.0000001, "1e-7"),
            (1.5e-9, "1.5e-9"),
        ] {
            assert_eq!(go_float(f), want, "{f:e}");
        }
    }
}
