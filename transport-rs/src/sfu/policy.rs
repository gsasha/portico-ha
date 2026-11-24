//! The answer-codec policy of `newSession`: G.722 and PCMU are registered with the pion
//! `MediaEngine`, Opus never, so the answer names, in the OFFERER'S order, the offered codecs
//! that match one of the two, and a phone that sends the first of them sends something this
//! package can decode (a phone offers Opus first; before this policy the hub decoded Opus bytes
//! as mu-law, noise and crackle at the gate speaker).
//!
//! Go leaves the decision to pion, so this is a model of pion v4.1.8 + pion/sdp v3.0.16 for ONE
//! audio media section, checked against the answers of a real `sfu.Session`
//! (`tests/golden/sfu_policy.json`, 41 offers). What it reproduces:
//!
//! * the payload types of the `m=audio` line, in order, each codec looked up by `rtpmap`
//!   (merged over the static table 0 = PCMU/8000, 8 = PCMA/8000, 9 = G722/8000: the first value
//!   of each field wins, so a `G722/16000` rtpmap on payload type 9 changes nothing);
//! * a match needs the same mime type (ASCII/Unicode case-folded), `ClockRateEqual` (a missing
//!   clock rate means the mime type's default: 8000 for PCMU/PCMA, 48000 Opus, 90000 for
//!   everything else, G.722 included, so a dynamic `G722` with no clock rate does NOT match) and
//!   `ChannelsEqual` (0 and 1 are the same; PCMU with 2 channels does not match);
//! * the answer repeats the offerer's payload type, name, clock rate and channels, and its
//!   `fmtp` line; a payload type listed twice is answered once;
//! * no matching codec at all is an error, as is a payload type with no codec at all (other than
//!   0), or one that is not a number below 256.
//!
//! Not modelled (no vector reaches them): an `fmtp` with `apt=` (RTX), more than one audio
//! section, a malformed SDP (pion's own parser would fail first, with other texts).

use crate::gostd::strconv::go_quote;

use super::audio::PhoneCodec;

/// One codec of an offer or an answer, as pion's `codecsFromMediaDescription` reads it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OfferedCodec {
    pub pt: u8,
    /// The encoding name as written in the rtpmap (the static name for 0, 8, 9).
    pub name: String,
    /// 0 when the rtpmap has no clock rate.
    pub clock: u32,
    /// 0 when the rtpmap has no channel count.
    pub channels: u16,
    /// The `fmtp` parameters after the payload type, or empty.
    pub fmtp: String,
}

impl OfferedCodec {
    /// `a=rtpmap:PT NAME/CLOCK[/CHANNELS]` as pion writes it in the answer.
    pub fn rtpmap_line(&self) -> String {
        let mut s = format!("a=rtpmap:{} {}/{}", self.pt, self.name, self.clock);
        if self.channels > 0 {
            s.push_str(&format!("/{}", self.channels));
        }
        s
    }

    /// `a=fmtp:PT PARAMS`, or `None` when there are none.
    pub fn fmtp_line(&self) -> Option<String> {
        (!self.fmtp.is_empty()).then(|| format!("a=fmtp:{} {}", self.pt, self.fmtp))
    }
}

/// Why `HandleOffer` fails on an offer, with the text Go gives.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PolicyError {
    /// `SetRemoteDescription` refused the offer (the pion text, e.g. `payload type not found`).
    SetRemote(String),
    /// `CreateAnswer` had no codec to offer.
    NoCommonCodec,
}

impl std::fmt::Display for PolicyError {
    /// The error text of `Session.HandleOffer`.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PolicyError::SetRemote(e) => write!(f, "sfu: setting remote offer: {e}"),
            PolicyError::NoCommonCodec => f.write_str(
                "sfu: creating answer: unable to populate media section, RTPSender created with no codecs",
            ),
        }
    }
}

impl std::error::Error for PolicyError {}

/// `strconv.ParseUint(s, 10, bits)` with Go's error text.
pub(crate) fn parse_uint(s: &str, bits: u32) -> Result<u64, String> {
    let syntax = || format!("strconv.ParseUint: parsing {}: invalid syntax", go_quote(s));
    if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
        return Err(syntax());
    }
    let max = (1u128 << bits) - 1;
    let mut n: u128 = 0;
    for b in s.bytes() {
        n = n * 10 + u128::from(b - b'0');
        if n > max {
            return Err(format!(
                "strconv.ParseUint: parsing {}: value out of range",
                go_quote(s)
            ));
        }
    }
    Ok(n as u64)
}

#[derive(Clone, Default)]
struct Raw {
    name: String,
    clock: u32,
    enc: String,
    fmtp: String,
}

/// pion/sdp `mergeCodecs`: a field already set is never overwritten.
fn merge(map: &mut std::collections::BTreeMap<u8, Raw>, pt: u8, add: Raw) {
    let c = map.entry(pt).or_default();
    if c.name.is_empty() {
        c.name = add.name;
    }
    if c.clock == 0 {
        c.clock = add.clock;
    }
    if c.enc.is_empty() {
        c.enc = add.enc;
    }
    if c.fmtp.is_empty() {
        c.fmtp = add.fmtp;
    }
}

/// pion/sdp `parseRtpmap` (`None` = the line is ignored).
fn parse_rtpmap(attr: &str) -> Option<(u8, Raw)> {
    // attr is "rtpmap:<pt> <name>/<clock>[/<params>]"
    let parts: Vec<&str> = attr.split(' ').collect();
    if parts.len() != 2 {
        return None;
    }
    let pt_parts: Vec<&str> = parts[0].split(':').collect();
    if pt_parts.len() != 2 {
        return None;
    }
    let pt = parse_uint(pt_parts[1], 8).ok()? as u8;
    let enc: Vec<&str> = parts[1].split('/').collect();
    let mut raw = Raw {
        name: enc[0].to_string(),
        ..Raw::default()
    };
    if enc.len() > 1 {
        raw.clock = parse_uint(enc[1], 32).ok()? as u32;
    }
    if enc.len() > 2 {
        raw.enc = enc[2].to_string();
    }
    Some((pt, raw))
}

/// pion/sdp `parseFmtp`.
fn parse_fmtp(attr: &str) -> Option<(u8, Raw)> {
    let (head, params) = attr.split_once(' ')?;
    let pt_parts: Vec<&str> = head.split(':').collect();
    if pt_parts.len() != 2 {
        return None;
    }
    let pt = parse_uint(pt_parts[1], 8).ok()? as u8;
    Some((
        pt,
        Raw {
            fmtp: params.to_string(),
            ..Raw::default()
        },
    ))
}

/// pion/sdp `parseRtcpFb`: the payload type of a non-wildcard `rtcp-fb` line.
fn parse_rtcp_fb_pt(attr: &str) -> Option<u8> {
    let (head, _) = attr.split_once(' ')?;
    let pt_parts: Vec<&str> = head.split(':').collect();
    if pt_parts.len() != 2 || pt_parts[1] == "*" {
        return None;
    }
    parse_uint(pt_parts[1], 8).ok().map(|v| v as u8)
}

/// The codecs of the FIRST `m=audio` section of `sdp`, in the order of its `m=` line
/// (`codecsFromMediaDescription`). `Ok(vec![])` when there is no audio section.
pub fn offered_audio_codecs(sdp: &str) -> Result<Vec<OfferedCodec>, PolicyError> {
    let mut formats: Option<Vec<String>> = None;
    let mut attrs: Vec<String> = Vec::new();
    for line in sdp.split('\n') {
        let line = line.strip_suffix('\r').unwrap_or(line);
        if let Some(rest) = line.strip_prefix("m=") {
            if formats.is_some() {
                break; // the next media section ends the first audio one
            }
            let f: Vec<&str> = rest.split(' ').collect();
            if f.len() >= 3 && f[0].eq_ignore_ascii_case("audio") {
                formats = Some(f[3..].iter().map(|s| s.to_string()).collect());
            }
        } else if formats.is_some() {
            if let Some(a) = line.strip_prefix("a=") {
                attrs.push(a.to_string());
            }
        }
    }
    let Some(formats) = formats else {
        return Ok(Vec::new());
    };

    // pion/sdp buildCodecMap: the static table, then the section's attributes in order.
    let mut map = std::collections::BTreeMap::new();
    for (pt, name) in [(0u8, "PCMU"), (8, "PCMA"), (9, "G722")] {
        map.insert(
            pt,
            Raw {
                name: name.to_string(),
                clock: 8000,
                ..Raw::default()
            },
        );
    }
    for a in &attrs {
        if a.starts_with("rtpmap:") {
            if let Some((pt, raw)) = parse_rtpmap(a) {
                merge(&mut map, pt, raw);
            }
        } else if a.starts_with("fmtp:") {
            if let Some((pt, raw)) = parse_fmtp(a) {
                merge(&mut map, pt, raw);
            }
        } else if a.starts_with("rtcp-fb:") {
            // Only the entry matters here: a feedback line for an unknown payload type makes
            // that payload type exist (with an empty name) instead of "not found".
            if let Some(pt) = parse_rtcp_fb_pt(a) {
                merge(&mut map, pt, Raw::default());
            }
        }
    }

    let mut out = Vec::new();
    for f in formats {
        let pt = parse_uint(&f, 8).map_err(PolicyError::SetRemote)? as u8;
        let Some(raw) = map.get(&pt) else {
            if pt == 0 {
                continue;
            }
            return Err(PolicyError::SetRemote("payload type not found".into()));
        };
        out.push(OfferedCodec {
            pt,
            name: raw.name.clone(),
            clock: raw.clock,
            channels: parse_uint(&raw.enc, 16).map_or(0, |v| v as u16),
            fmtp: raw.fmtp.clone(),
        });
    }
    Ok(out)
}

/// pion `fmtp.ClockRateEqual`/`defaultClockRate`.
fn default_clock(mime_lower: &str) -> u32 {
    match mime_lower {
        "audio/opus" => 48000,
        "audio/pcmu" | "audio/pcma" => 8000,
        _ => 90000,
    }
}

fn clock_equal(mime_lower: &str, a: u32, b: u32) -> bool {
    let d = default_clock(mime_lower);
    (if a == 0 { d } else { a }) == (if b == 0 { d } else { b })
}

/// pion `fmtp.ChannelsEqual` (only Opus defaults to 2; 0 means 1).
fn channels_equal(mime_lower: &str, a: u16, b: u16) -> bool {
    let d = if mime_lower == "audio/opus" { 2 } else { 0 };
    let norm = |v: u16| match if v == 0 { d } else { v } {
        0 => 1,
        v => v,
    };
    norm(a) == norm(b)
}

/// pion `fmtp.parseParameters` (keys lower-cased; a line without `=` has an empty value).
fn fmtp_params(line: &str) -> Vec<(String, String)> {
    line.split(';')
        .map(|p| {
            let p = p.trim();
            match p.split_once('=') {
                Some((k, v)) => (k.to_lowercase(), v.to_string()),
                None => (p.to_lowercase(), String::new()),
            }
        })
        .collect()
}

/// `paramsEqual` of the generic fmtp against an empty local fmtp, whose parse is `{"": ""}`: it
/// fails only if the remote has the key `""` with a non-empty value.
fn params_equal_local_empty(remote_fmtp: &str) -> bool {
    !fmtp_params(remote_fmtp)
        .iter()
        .any(|(k, v)| k.is_empty() && !v.is_empty())
}

/// The two codecs `newSession` registers, as `(mime lower-case, payload type)`.
const REGISTERED: [(&str, u8); 2] = [("audio/g722", 9), ("audio/pcmu", 0)];

/// `answer_codecs` for already parsed codecs: exact matches (mime, clock, channels, parameters)
/// when there are any, otherwise partial ones (mime, clock, channels), in the offerer's order,
/// one entry per payload type.
pub fn select_answer_codecs(offered: &[OfferedCodec]) -> Vec<OfferedCodec> {
    let mut exact: Vec<OfferedCodec> = Vec::new();
    let mut partial: Vec<OfferedCodec> = Vec::new();
    for o in offered {
        let mime = format!("audio/{}", o.name).to_lowercase();
        let fuzzy = REGISTERED.iter().any(|(m, _)| {
            *m == mime && clock_equal(m, 8000, o.clock) && channels_equal(m, 0, o.channels)
        });
        if !fuzzy {
            continue;
        }
        let list = if params_equal_local_empty(&o.fmtp) {
            &mut exact
        } else {
            &mut partial
        };
        if !list.iter().any(|c| c.pt == o.pt) {
            list.push(o.clone());
        }
    }
    if exact.is_empty() {
        partial
    } else {
        exact
    }
}

/// What the answer to `sdp` carries: the offered G.722/PCMU codecs in the offerer's order, or
/// the error `HandleOffer` returns.
pub fn answer_codecs(sdp: &str) -> Result<Vec<OfferedCodec>, PolicyError> {
    let offered = offered_audio_codecs(sdp)?;
    let answer = select_answer_codecs(&offered);
    if answer.is_empty() {
        return Err(PolicyError::NoCommonCodec);
    }
    Ok(answer)
}

/// `audioTrack.Bind`: the codec the phone is sent is the first of the negotiated list this
/// package can encode (G.722 or PCMU); `None` is `ErrUnsupportedCodec`. PCMU until bound.
pub fn bind_codec(answer: &[OfferedCodec]) -> Option<PhoneCodec> {
    answer
        .iter()
        .find_map(|c| match c.name.to_lowercase().as_str() {
            "g722" => Some(PhoneCodec::G722),
            "pcmu" => Some(PhoneCodec::Pcmu),
            _ => None,
        })
}
