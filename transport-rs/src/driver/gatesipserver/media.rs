//! `media.go`: codec choice and the A-law path. A port of gate-sip's media.go and its G.711
//! routines, with the same hard-won rules:
//!
//! * The answerer may only pick from what the offer lists.
//! * Prefer 16 kHz LINEAR, whatever the offer calls it (`PCM/16000` from the station,
//!   `L16/16000` from the RFC). `G722/16000` is a codec we cannot decode.
//! * The wideband payload is LITTLE-endian, against RFC 3551, so the answer advertises the
//!   vendor's own name `PCM/16000`, never `L16/16000`.
//! * PCMU is deliberately absent: there is no mu-law codec here.
//! * Nothing carriable offered: answer PCMA anyway (the pre-negotiation behaviour) and say so.
//!
//! Go reads the offer with four regular expressions; they are re-implemented here by hand with
//! RE2's semantics (`\s` is ASCII `[\t\n\f\r ]`, `\d` is ASCII, `.` excludes only `\n`, `(?m)`
//! anchors at `\n`), because there is no `regex` dependency. The golden vectors replay
//! every odd shape the Go regexps accept (a payload list on the line after `RTP/AVP`, an
//! rtpmap name that spans a newline, ...).

use std::collections::HashMap;
use std::sync::OnceLock;

use crate::gostd::strconv::{go_atoi, go_atoi_lenient};

/// Go's unexported `media`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Media {
    /// RTP payload type
    pub pt: i64,
    /// a=rtpmap name/rate
    pub name: String,
    /// samples per second
    pub rate: u32,
    /// PCM16 bytes in ONE 20 ms client frame
    pub frame_bytes: usize,
    /// true: transcode A-law <-> PCM16; false: pass through
    pub alaw: bool,
}

pub fn media_wide() -> Media {
    Media {
        pt: 96,
        name: "PCM/16000".to_string(),
        rate: 16000,
        frame_bytes: 640,
        alaw: false,
    }
}

pub fn media_narrow() -> Media {
    Media {
        pt: 8,
        name: "PCMA/8000".to_string(),
        rate: 8000,
        frame_bytes: 320,
        alaw: true,
    }
}

fn is_re_space(b: u8) -> bool {
    matches!(b, b'\t' | b'\n' | 0x0c | b'\r' | b' ')
}

/// Length of the run of bytes at `from` satisfying `f`.
fn run(b: &[u8], from: usize, f: impl Fn(u8) -> bool) -> usize {
    b[from.min(b.len())..].iter().take_while(|&&c| f(c)).count()
}

/// The start of every line (`(?m)^`): 0 and each position after a `\n`.
fn line_starts(b: &[u8]) -> impl Iterator<Item = usize> + '_ {
    std::iter::once(0)
        .chain(
            b.iter()
                .enumerate()
                .filter(|(_, &c)| c == b'\n')
                .map(|(i, _)| i + 1),
        )
        .filter(move |&i| i <= b.len())
}

/// `(?m)^m=audio\s+(\d+)\s+RTP/AVP\s+(.+)$`: the second group of the first match.
fn match_m_line(sdp: &str) -> Option<&str> {
    let b = sdp.as_bytes();
    for p in line_starts(b) {
        if !b[p..].starts_with(b"m=audio") {
            continue;
        }
        let mut q = p + 7;
        let ws = run(b, q, is_re_space);
        if ws == 0 {
            continue;
        }
        q += ws;
        let digits = run(b, q, |c| c.is_ascii_digit());
        if digits == 0 {
            continue;
        }
        q += digits;
        let ws = run(b, q, is_re_space);
        if ws == 0 {
            continue;
        }
        q += ws;
        if !b[q..].starts_with(b"RTP/AVP") {
            continue;
        }
        q += 7;
        // `\s+` is greedy but gives characters back until `.+` has something to eat.
        let l = run(b, q, is_re_space);
        for k in (1..=l).rev() {
            let r = q + k;
            if r < b.len() && b[r] != b'\n' {
                let end = b[r..]
                    .iter()
                    .position(|&c| c == b'\n')
                    .map_or(b.len(), |i| r + i);
                return Some(&sdp[r..end]);
            }
        }
    }
    None
}

/// `FindAllStringSubmatch` of `(?m)^a=rtpmap:(\d+)\s+([^/]+)/(\d+)`: (payload, name, rate).
fn find_rtpmaps(sdp: &str) -> Vec<(&str, &str, &str)> {
    let b = sdp.as_bytes();
    let mut out = Vec::new();
    let mut next_ok = 0usize;
    for p in line_starts(b) {
        if p < next_ok || !b[p..].starts_with(b"a=rtpmap:") {
            continue;
        }
        let d0 = p + 9;
        let dl = run(b, d0, |c| c.is_ascii_digit());
        if dl == 0 {
            continue;
        }
        let w0 = d0 + dl;
        let l = run(b, w0, is_re_space);
        if l == 0 {
            continue;
        }
        // `[^/]+` runs to the next '/', which is where the name must end.
        let Some(slash) = b[w0 + l..]
            .iter()
            .position(|&c| c == b'/')
            .map(|i| w0 + l + i)
        else {
            continue;
        };
        // the name starts after k whitespace characters, k as large as leaves it non-empty
        let name_start = if slash > w0 + l {
            w0 + l
        } else if l >= 2 {
            w0 + l - 1
        } else {
            continue;
        };
        let rl = run(b, slash + 1, |c| c.is_ascii_digit());
        if rl == 0 {
            continue;
        }
        let end = slash + 1 + rl;
        out.push((&sdp[d0..w0], &sdp[name_start..slash], &sdp[slash + 1..end]));
        next_ok = end;
    }
    out
}

/// `strings.ToUpper(s)` hasPrefix helper.
fn upper_starts_with(s: &str, prefix: &str) -> bool {
    s.to_uppercase().starts_with(prefix)
}

/// `chooseMedia`: the codec to answer with and a one-line note for the log.
pub fn choose_media(sdp: &str) -> (Media, String) {
    let Some(offer) = match_m_line(sdp) else {
        return (
            media_narrow(),
            "no m=audio line — assuming PCMA".to_string(),
        );
    };
    let offered: Vec<&str> = offer.split_whitespace().collect();
    let mut maps: HashMap<&str, String> = HashMap::new();
    for (pt, name, rate) in find_rtpmaps(sdp) {
        maps.insert(pt, format!("{name}/{rate}"));
    }
    for pt in &offered {
        let Some(d) = maps.get(pt) else { continue };
        if !d.ends_with("/16000") {
            continue;
        }
        if !upper_starts_with(d, "L16") && !upper_starts_with(d, "PCM/") {
            continue; // e.g. G722/16000
        }
        let Ok(n) = go_atoi(pt) else { continue };
        let mut w = media_wide();
        w.pt = n;
        w.name = d.clone();
        return (w, format!("wideband: payload {n} = {d}"));
    }
    for pt in &offered {
        let is_pcma = maps.get(pt).is_some_and(|d| upper_starts_with(d, "PCMA"));
        if *pt == "8" || is_pcma {
            // `n, _ := strconv.Atoi(pt)`: a range error keeps the clamped value (an offer whose PCMA
            // payload is `99999999999999999999` is answered `RTP/AVP 9223372036854775807`), a
            // syntax error is 0 (the RTP payload type is then 255, as in Go)
            let n = go_atoi_lenient(pt);
            let mut nb = media_narrow();
            if n > 0 {
                nb.pt = n;
            }
            let note = format!("narrowband: payload {} = PCMA/8000", nb.pt);
            return (nb, note);
        }
    }
    (
        media_narrow(),
        format!("offer had nothing we speak ({offer}) — answering PCMA"),
    )
}

/// `sdpFor`: the answer, with exactly ONE payload (echoing the whole offer back is the
/// classic bug that looks fine until the far end picks the other one).
pub fn sdp_for(local_ip: &str, rtp_port: i64, m: &Media) -> String {
    format!(
        "v=0\r\no=- 0 0 IN IP4 {local_ip}\r\ns=portico-gate-sip-server\r\nc=IN IP4 {local_ip}\r\n\
         t=0 0\r\nm=audio {rtp_port} RTP/AVP {pt}\r\na=rtpmap:{pt} {name}\r\na=sendrecv\r\n",
        pt = m.pt,
        name = m.name
    )
}

// ── G.711 A-law (ITU-T G.191 / Sun g711.c shape, as in gate-sip/audio.go) ──

struct AlawTables {
    to_pcm: [i16; 256],
    /// indexed by `(sample >> 3) + 4096`
    from_pcm: Vec<u8>,
}

fn tables() -> &'static AlawTables {
    static T: OnceLock<AlawTables> = OnceLock::new();
    T.get_or_init(|| {
        let mut to_pcm = [0i16; 256];
        for (i, v) in to_pcm.iter_mut().enumerate() {
            *v = decode_alaw(i as u8);
        }
        let from_pcm = (0..8192)
            .map(|i| encode_alaw(((i - 4096) << 3) as i16))
            .collect();
        AlawTables { to_pcm, from_pcm }
    })
}

pub fn decode_alaw(a: u8) -> i16 {
    let a = a ^ 0x55;
    let mut t = i32::from(a & 0x0f) << 4;
    let seg = i32::from(a & 0x70) >> 4;
    match seg {
        0 => t += 8,
        1 => t += 0x108,
        _ => {
            t += 0x108;
            t <<= seg - 1;
        }
    }
    if a & 0x80 != 0 {
        t as i16
    } else {
        (-t) as i16
    }
}

const SEG_A_END: [i32; 8] = [0x1f, 0x3f, 0x7f, 0xff, 0x1ff, 0x3ff, 0x7ff, 0xfff];

pub fn encode_alaw(pcm: i16) -> u8 {
    let mut v = i32::from(pcm) >> 3;
    let mask: i32;
    if v >= 0 {
        mask = 0xd5;
    } else {
        mask = 0x55;
        v = -v - 1;
    }
    let mut seg = 8usize;
    for (i, &end) in SEG_A_END.iter().enumerate() {
        if v <= end {
            seg = i;
            break;
        }
    }
    if seg >= 8 {
        return (0x7f ^ mask) as u8;
    }
    let mut aval = (seg as i32) << 4;
    if seg < 2 {
        aval |= (v >> 1) & 0x0f;
    } else {
        aval |= (v >> seg) & 0x0f;
    }
    (aval ^ mask) as u8
}

/// A-law bytes to PCM16 little-endian.
pub fn alaw_decode(input: &[u8]) -> Vec<u8> {
    let t = tables();
    let mut out = Vec::with_capacity(input.len() * 2);
    for &c in input {
        out.extend_from_slice(&t.to_pcm[usize::from(c)].to_le_bytes());
    }
    out
}

/// PCM16 little-endian to A-law bytes (a trailing odd byte is ignored).
pub fn alaw_encode(pcm: &[u8]) -> Vec<u8> {
    let t = tables();
    pcm.as_chunks::<2>()
        .0
        .iter()
        .map(|b| {
            let s = i16::from_le_bytes([b[0], b[1]]);
            t.from_pcm[((i32::from(s) >> 3) + 4096) as usize]
        })
        .collect()
}

// ── the two SDP lines the SIP server reads from an INVITE / 200 body (server.go `cIPRe`, `mPortRe`) ──

/// `(?m)^c=IN IP4 (\S+)`: the first such line's address text.
pub(crate) fn find_c_ip(body: &str) -> Option<&str> {
    let b = body.as_bytes();
    let mut p = 0;
    loop {
        if b[p..].starts_with(b"c=IN IP4 ") {
            let s = p + 9;
            let e = b[s..]
                .iter()
                .position(|c| matches!(c, b'\t' | b'\n' | 0x0c | b'\r' | b' '))
                .map_or(b.len(), |i| s + i);
            if e > s {
                return Some(&body[s..e]);
            }
        }
        p += b[p..].iter().position(|&c| c == b'\n')? + 1;
    }
}

/// `(?m)^m=audio (\d+)` followed by `fmt.Sscanf("%d")`: the port, 0 when the digits overflow an
/// `int64` (Sscanf then fails and leaves its target alone).
pub(crate) fn find_m_port(body: &str) -> Option<(i64, bool)> {
    let b = body.as_bytes();
    let mut p = 0;
    loop {
        if b[p..].starts_with(b"m=audio ") {
            let s = p + 8;
            let e = b[s..]
                .iter()
                .position(|c| !c.is_ascii_digit())
                .map_or(b.len(), |i| s + i);
            if e > s {
                return Some(match body[s..e].parse::<i64>() {
                    Ok(n) => (n, false),
                    Err(_) => (0, true),
                });
            }
        }
        p += b[p..].iter().position(|&c| c == b'\n')? + 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // TestAlawKnownValues (misc_test.go)
    #[test]
    fn alaw_known_values() {
        assert_eq!(alaw_encode(&[0, 0]), vec![0xD5], "A-law silence");
        assert_eq!(decode_alaw(0xD5), 8);
        assert_eq!(decode_alaw(0x55), -8);
        let mut v = -32768i32;
        while v <= 32767 {
            let d = i32::from(decode_alaw(encode_alaw(v as i16)));
            let tol = 16 + v.abs() / 16; // the quantisation error A-law actually permits
            assert!(
                (d - v).abs() <= tol,
                "round trip {v} -> {d}, error beyond {tol}"
            );
            v += 17;
        }
    }

    #[test]
    fn regex_shapes_match_re2() {
        // the payload list on the line after `RTP/AVP` (\s+ spans the newline)
        let sdp = "m=audio 5004 RTP/AVP\r\na=rtpmap:96 PCM/16000\r\n";
        assert_eq!(match_m_line(sdp), Some("a=rtpmap:96 PCM/16000\r"));
        assert_eq!(match_m_line("m=audio 5004 RTP/AVP\r\n"), None);
        assert_eq!(match_m_line("m=audio 5004 RTP/AVP \t\r\n8"), Some("8"));
        assert_eq!(match_m_line("m=audio 5004 RTP/AVP"), None);
        assert_eq!(match_m_line(" m=audio 5004 RTP/AVP 8"), None);
        let maps = find_rtpmaps("a=rtpmap:96   /16000\r\na=rtpmap:8 PCMA/8000\r\n");
        assert_eq!(maps, vec![("96", " ", "16000"), ("8", "PCMA", "8000")]);
    }

    // R1-84: Go's `n, _ := strconv.Atoi(pt)` keeps the clamped value on a range error.
    #[test]
    fn an_overflowing_pcma_payload_is_clamped_as_in_go() {
        let sdp = "v=0\r\nm=audio 5004 RTP/AVP 99999999999999999999\r\n\
                   a=rtpmap:99999999999999999999 PCMA/8000\r\n";
        let (m, note) = choose_media(sdp);
        assert_eq!(m.pt, i64::MAX, "{note}");
        assert!(note.contains("9223372036854775807"), "{note}");
        assert!(sdp_for("127.0.0.1", 40000, &m).contains("RTP/AVP 9223372036854775807"));
    }
}
