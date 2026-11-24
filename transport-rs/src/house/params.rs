//! The `house_open` parameters: a hand-written decoder with Go's `json.Unmarshal` error texts (`houseOpenParams`, `houseLegParams`).

use super::LegSpec;

pub(super) const OPEN_STRUCT: &str = "houseOpenParams";
pub(super) const LEG_STRUCT: &str = "houseLegParams";

/// The parameters of `house_open`, decoded as Go's `json.Unmarshal` into `houseOpenParams` does.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct OpenParams {
    pub call_id: String,
    pub legs: Vec<LegSpec>,
}

fn kind_of(text: &str) -> &'static str {
    match text.as_bytes().first() {
        Some(b'"') => "string",
        Some(b'{') => "object",
        Some(b'[') => "array",
        Some(b't') | Some(b'f') => "bool",
        _ => "number",
    }
}
fn entries(text: &str) -> Vec<(String, String)> {
    crate::ipc::parse_object_entries(text.as_bytes())
        .unwrap_or_default()
        .into_iter()
        .map(|(k, v)| (k, v.get().to_string()))
        .collect()
}
fn pick(specs: &[&str], key: &str) -> Option<usize> {
    specs
        .iter()
        .position(|s| *s == key)
        .or_else(|| specs.iter().position(|s| s.eq_ignore_ascii_case(key)))
}

/// The `legs` array of `house_open`: one `LegSpec` per element, a type error noted once and the
/// element still counted, as Go's decoder goes on after it.
fn decode_legs(text: &str, note: &mut impl FnMut(String)) -> Vec<LegSpec> {
    let mut legs = Vec::new();
    let items: Vec<Box<serde_json::value::RawValue>> =
        serde_json::from_str(text).unwrap_or_default();
    for item in items {
        let it = item.get();
        let mut leg = LegSpec::default();
        if it == "null" {
            legs.push(leg);
            continue;
        }
        if !it.starts_with('{') {
            note(format!(
                "json: cannot unmarshal {} into Go struct field {OPEN_STRUCT}.legs of type main.{LEG_STRUCT}",
                kind_of(it)
            ));
            legs.push(leg);
            continue;
        }
        decode_leg_fields(it, &mut leg, note);
        legs.push(leg);
    }
    legs
}

/// The members of one leg object.
fn decode_leg_fields(it: &str, leg: &mut LegSpec, note: &mut impl FnMut(String)) {
    const NAMES: [&str; 5] = ["side", "kind", "codec", "sdp", "owner"];
    for (k, v) in entries(it) {
        let Some(i) = pick(&NAMES, &k) else {
            continue;
        };
        if v == "null" {
            continue;
        }
        if kind_of(&v) != "string" {
            note(format!(
                "json: cannot unmarshal {} into Go struct field {LEG_STRUCT}.legs.{} of type string",
                kind_of(&v),
                NAMES[i]
            ));
            continue;
        }
        let s = unquote(&v);
        match i {
            0 => leg.side = s,
            1 => leg.kind = s,
            2 => leg.codec = s,
            3 => leg.sdp = s,
            _ => leg.owner = s,
        }
    }
}

/// `json.Unmarshal(raw, &houseOpenParams{})`: the fields it could decode and Go's error text (the
/// first error, in document order; decoding goes on after a type error, as Go's does).
pub fn decode_open_params(raw: &str) -> Result<OpenParams, String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err("unexpected end of JSON input".into());
    }
    let mut out = OpenParams::default();
    if trimmed == "null" {
        return Ok(out);
    }
    if !trimmed.starts_with('{') {
        return Err(format!(
            "json: cannot unmarshal {} into Go value of type main.{OPEN_STRUCT}",
            kind_of(trimmed)
        ));
    }
    let mut first_err: Option<String> = None;
    let mut note = |e: String| {
        if first_err.is_none() {
            first_err = Some(e);
        }
    };
    for (key, text) in entries(trimmed) {
        match pick(&["call_id", "legs"], &key) {
            None => {}
            Some(0) => {
                if text == "null" {
                    continue;
                }
                if kind_of(&text) == "string" {
                    out.call_id = unquote(&text);
                } else {
                    note(format!(
                        "json: cannot unmarshal {} into Go struct field {OPEN_STRUCT}.call_id of type string",
                        kind_of(&text)
                    ));
                }
            }
            Some(_) => {
                if text == "null" {
                    out.legs.clear();
                    continue;
                }
                if kind_of(&text) != "array" {
                    note(format!(
                        "json: cannot unmarshal {} into Go struct field {OPEN_STRUCT}.legs of type []main.{LEG_STRUCT}",
                        kind_of(&text)
                    ));
                    continue;
                }
                out.legs = decode_legs(&text, &mut note);
            }
        }
    }
    match first_err {
        Some(e) => Err(e),
        None => Ok(out),
    }
}

pub(super) fn unquote(text: &str) -> String {
    // lone surrogate escapes become U+FFFD, as in Go
    crate::gojson::unquote(text)
}
