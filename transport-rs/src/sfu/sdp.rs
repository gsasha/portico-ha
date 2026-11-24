//! SDP text handling around `str0m`: the offer rewrite and size cap of the S1b port conditions,
//! the first-line syntax error pion writes, and the helpers that read the answer str0m produced.
//! Nothing here parses SDP in general: str0m does that.

/// S1b condition (b): an offer longer than this is refused before it is parsed. A browser offer
/// is 1 to 6 KB and 30 to 120 lines.
pub const MAX_OFFER_BYTES: usize = 64 * 1024;
/// S1b condition (b): and an offer with more lines than this.
pub const MAX_OFFER_LINES: usize = 1000;

/// `Err(text)` when the offer is beyond what a browser ever sends.
pub fn check_offer_size(sdp: &str) -> Result<(), String> {
    let lines = sdp.matches('\n').count();
    if sdp.len() > MAX_OFFER_BYTES || lines > MAX_OFFER_LINES {
        return Err(format!(
            "offer too large ({} bytes, {} lines; the limits are {} bytes and {} lines)",
            sdp.len(),
            lines,
            MAX_OFFER_BYTES,
            MAX_OFFER_LINES
        ));
    }
    Ok(())
}

/// S1b condition (a): `a=rtpmap:<pt> <name>/<rate>/1` becomes `a=rtpmap:<pt> <name>/<rate>`.
/// str0m 0.25 scores a channel count of `Some(1)` differently from `None` and then refuses
/// `PCMU/8000/1` (Firefox writes `G722/8000/1`). Only a trailing `/1` of an rtpmap line is
/// removed; `opus/48000/2` and every other line are untouched, line endings are kept.
pub fn normalise_rtpmap(sdp: &str) -> String {
    let mut out = String::with_capacity(sdp.len());
    for line in sdp.split_inclusive('\n') {
        let (body, eol) = match line.strip_suffix("\r\n") {
            Some(b) => (b, "\r\n"),
            None => match line.strip_suffix('\n') {
                Some(b) => (b, "\n"),
                None => (line, ""),
            },
        };
        if body.starts_with("a=rtpmap:") && body.ends_with("/1") {
            out.push_str(&body[..body.len() - 2]);
        } else {
            out.push_str(body);
        }
        out.push_str(eol);
    }
    out
}

/// Go's `strconv.QuoteToASCII` of ONE byte (the way pion/sdp's `syntaxError.Error` quotes the
/// offending byte: a non-ASCII byte is an invalid one-byte string and prints as `\xNN`).
fn quote_byte(b: u8) -> String {
    match b {
        b'"' => "\"\\\"\"".to_string(),
        b'\\' => "\"\\\\\"".to_string(),
        0x07 => "\"\\a\"".to_string(),
        0x08 => "\"\\b\"".to_string(),
        0x0c => "\"\\f\"".to_string(),
        b'\n' => "\"\\n\"".to_string(),
        b'\r' => "\"\\r\"".to_string(),
        b'\t' => "\"\\t\"".to_string(),
        0x0b => "\"\\v\"".to_string(),
        0x20..=0x7e => format!("\"{}\"", b as char),
        _ => format!("\"\\x{b:02x}\""),
    }
}

/// What pion/sdp's lexer says about the FIRST line of a text that is not an SDP, without the
/// `failed to unmarshal SDP: ` prefix pion/webrtc adds. Only the first line is modelled
/// (`readType`: skip newlines, a key byte, then `=`; the first key must be `v`); a later
/// syntax error is str0m's to report and its text differs. `None` means "looks like the start
/// of an SDP, let str0m parse it".
pub fn pion_first_line_error(sdp: &str) -> Option<String> {
    let b = sdp.as_bytes();
    let mut i = 0;
    while i < b.len() && (b[i] == b'\n' || b[i] == b'\r') {
        i += 1;
    }
    if i >= b.len() {
        return None;
    }
    if i + 1 >= b.len() {
        return Some("EOF".to_string());
    }
    let idx = i + 1;
    if b[idx] != b'=' || b[i] != b'v' {
        return Some(format!(
            "sdp: syntax error at pos {}: {}",
            idx,
            quote_byte(b[idx])
        ));
    }
    None
}

/// The payload types of the first `m=audio` line of an OFFER, in order; a format that is not a
/// number 0 to 255 is an error with Go's `strconv.ParseUint` text, as pion refuses such an offer
/// (R1-106). `Ok(vec![])` when there is no audio section.
pub fn try_audio_payload_types(sdp: &str) -> Result<Vec<u8>, String> {
    for l in sdp.split('\n') {
        let l = l.trim_end_matches('\r');
        if l.starts_with("m=audio ") {
            return l
                .split_whitespace()
                .skip(3)
                .map(|f| super::policy::parse_uint(f, 8).map(|n| n as u8))
                .collect();
        }
    }
    Ok(Vec::new())
}

/// The payload types of the first `m=audio` line of `sdp`, in order (formats that are not a
/// `u8` are skipped: this is for an ANSWER, which str0m wrote; an offer goes through
/// [`try_audio_payload_types`]).
pub fn audio_payload_types(sdp: &str) -> Vec<u8> {
    for l in sdp.split('\n') {
        let l = l.trim_end_matches('\r');
        if l.starts_with("m=audio ") {
            return l
                .split_whitespace()
                .skip(3)
                .filter_map(|f| f.parse::<u8>().ok())
                .collect();
        }
    }
    Vec::new()
}

/// Puts the payload types of the first `m=audio` line of `sdp` in the OFFERER's order (the
/// types of `order`, the offer's own `m=audio` list), as pion does: the answer's first payload
/// type is what the offerer sends, so an offerer that lists G.722 first is sent G.722 and one
/// that lists PCMU first is sent PCMU. str0m answers in its own preference order (G.722 before
/// PCMU), which would override a phone that prefers PCMU. A type the answer has that the offer did
/// not list is DROPPED (RFC 3264: an answer names only offered formats; str0m also negotiates
/// from the rtpmap lines when the `m=` line leaves a type out, which pion does not).
pub fn order_audio_pts(sdp: &str, order: &[u8]) -> String {
    let mut out = String::with_capacity(sdp.len());
    let mut done = false;
    let mut dropped: Vec<u8> = Vec::new();
    for line in sdp.split_inclusive('\n') {
        if !done && line.starts_with("m=audio ") {
            done = true;
            let (body, eol) = match line.strip_suffix("\r\n") {
                Some(b) => (b, "\r\n"),
                None => match line.strip_suffix('\n') {
                    Some(b) => (b, "\n"),
                    None => (line, ""),
                },
            };
            let f: Vec<&str> = body.split(' ').collect();
            if f.len() > 3 {
                let answered: Vec<u8> = f[3..].iter().filter_map(|x| x.parse().ok()).collect();
                let mut pts: Vec<u8> = Vec::new();
                for p in order {
                    if answered.contains(p) && !pts.contains(p) {
                        pts.push(*p);
                    }
                }
                dropped = answered
                    .iter()
                    .copied()
                    .filter(|p| !pts.contains(p))
                    .collect();
                out.push_str(&f[..3].join(" "));
                for p in pts {
                    out.push(' ');
                    out.push_str(&p.to_string());
                }
                out.push_str(eol);
                continue;
            }
        }
        out.push_str(line);
    }
    if dropped.is_empty() {
        return out;
    }
    // and the attribute lines of a dropped type go with it, so the answer is clean; only the
    // lines of the audio section (up to the next `m=`): another section may use the same number
    let gone = |l: &str| {
        dropped.iter().any(|p| {
            ["a=rtpmap:", "a=fmtp:", "a=rtcp-fb:"]
                .iter()
                .any(|k| l.starts_with(&format!("{k}{p} ")))
        })
    };
    // (R2-89: only the FIRST audio section was rewritten above, so only its lines are filtered)
    let (mut in_audio, mut seen_audio) = (false, false);
    out.split_inclusive('\n')
        .filter(|l| {
            if l.starts_with("m=") {
                in_audio = l.starts_with("m=audio ") && !seen_audio;
                seen_audio |= in_audio;
            }
            !(in_audio && gone(l))
        })
        .collect()
}

/// A str0m offer-parse error without the pointer value it prints (`PointerOffset(0x7f..)` is a
/// memory address and differs on every run).
pub fn stable_error(e: &str) -> String {
    match e.find("PointerOffset(") {
        Some(i) => format!("{}an offset in the text", &e[..i]),
        None => e.to_string(),
    }
}

/// Whether the first audio section of an SDP is rejected (port 0).
pub fn audio_rejected(sdp: &str) -> bool {
    sdp.split('\n')
        .map(|l| l.trim_end_matches('\r'))
        .find(|l| l.starts_with("m=audio "))
        .map(|l| l.split_whitespace().nth(1) == Some("0"))
        .unwrap_or(true)
}

/// The answer is non-trickle (every host candidate is already in it): say so with
/// `a=end-of-candidates`, as pion does, right after the last candidate line. An answer that
/// has no candidate or already carries the marker is returned unchanged.
pub fn with_end_of_candidates(answer: &str) -> String {
    if answer.contains("a=end-of-candidates") {
        return answer.to_string();
    }
    let mut lines: Vec<&str> = answer.split_inclusive('\n').collect();
    let Some(last) = lines.iter().rposition(|l| l.starts_with("a=candidate:")) else {
        return answer.to_string();
    };
    let eol = if lines[last].ends_with("\r\n") {
        "\r\n"
    } else {
        "\n"
    };
    let marker = format!("a=end-of-candidates{eol}");
    let fixed: String;
    if !lines[last].ends_with('\n') {
        fixed = format!("{}{}", lines[last], "\r\n");
        lines[last] = &fixed;
    }
    let mut out = String::with_capacity(answer.len() + 24);
    for (i, l) in lines.iter().enumerate() {
        out.push_str(l);
        if i == last {
            out.push_str(&marker);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rtpmap_slash_one_is_stripped_only_where_it_is_the_channel_count() {
        let sdp = "a=rtpmap:9 G722/8000/1\r\na=rtpmap:111 opus/48000/2\r\na=rtpmap:0 PCMU/8000/1\na=fmtp:1 x/1\r\na=rtpmap:8 PCMA/8000";
        assert_eq!(
            normalise_rtpmap(sdp),
            "a=rtpmap:9 G722/8000\r\na=rtpmap:111 opus/48000/2\r\na=rtpmap:0 PCMU/8000\na=fmtp:1 x/1\r\na=rtpmap:8 PCMA/8000"
        );
    }

    #[test]
    fn the_size_cap_is_64_kib_and_1000_lines() {
        assert!(check_offer_size(&"a=x\r\n".repeat(1000)).is_ok());
        assert!(check_offer_size(&"a=x\r\n".repeat(1001)).is_err());
        assert!(check_offer_size(&"a".repeat(MAX_OFFER_BYTES)).is_ok());
        assert!(check_offer_size(&"a".repeat(MAX_OFFER_BYTES + 1)).is_err());
    }

    // The texts are what the real Go binary answered (pion/sdp v3.0.16), see PARITY.md.
    #[test]
    fn first_line_errors_are_pions() {
        assert_eq!(
            pion_first_line_error("garbage").as_deref(),
            Some("sdp: syntax error at pos 1: \"a\"")
        );
        assert_eq!(
            pion_first_line_error("x=1\r\n").as_deref(),
            Some("sdp: syntax error at pos 1: \"=\"")
        );
        assert_eq!(
            pion_first_line_error("\r\n\r\nzz").as_deref(),
            Some("sdp: syntax error at pos 5: \"z\"")
        );
        assert_eq!(pion_first_line_error("g").as_deref(), Some("EOF"));
        assert_eq!(pion_first_line_error("v=0\r\n"), None);
        assert_eq!(pion_first_line_error(""), None);
        assert_eq!(
            pion_first_line_error("v\u{e9}").as_deref(),
            Some("sdp: syntax error at pos 1: \"\\xc3\"")
        );
    }

    // R1-105: the attribute lines of a dropped type go from the audio section only
    #[test]
    fn a_dropped_payload_type_loses_its_lines_in_the_audio_section_only() {
        let a = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0 8\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:8 PCMA/8000\r\n\
                 a=rtcp-fb:8 nack\r\nm=video 9 UDP/TLS/RTP/SAVPF 8\r\na=rtpmap:8 H264/90000\r\na=fmtp:8 x=1\r\n";
        // the offer listed only 0: type 8 is dropped from the audio section
        let got = order_audio_pts(a, &[0]);
        assert_eq!(
            got,
            "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\na=rtpmap:0 PCMU/8000\r\n\
             m=video 9 UDP/TLS/RTP/SAVPF 8\r\na=rtpmap:8 H264/90000\r\na=fmtp:8 x=1\r\n"
        );
    }

    // R2-89: a second audio section is left alone, as its `m=` line is
    #[test]
    fn a_second_audio_section_keeps_its_lines() {
        let a = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0 96\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:96 G722/8000\r\n\
                 m=audio 9 UDP/TLS/RTP/SAVPF 0 96\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:96 G722/8000\r\n";
        let got = order_audio_pts(a, &[0]);
        assert_eq!(
            got,
            "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\na=rtpmap:0 PCMU/8000\r\n\
             m=audio 9 UDP/TLS/RTP/SAVPF 0 96\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:96 G722/8000\r\n"
        );
    }

    // R1-106: an offer format that is no number 0 to 255 is an error, as pion's ParseUint is
    #[test]
    fn a_non_numeric_audio_format_is_an_error_not_a_shorter_list() {
        let ok = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111 9 0\r\n";
        assert_eq!(try_audio_payload_types(ok), Ok(vec![111, 9, 0]));
        assert_eq!(try_audio_payload_types("v=0\r\n"), Ok(vec![]));
        assert_eq!(
            try_audio_payload_types("m=audio 9 UDP/TLS/RTP/SAVPF 0 99999\r\n"),
            Err("strconv.ParseUint: parsing \"99999\": value out of range".to_string())
        );
        assert_eq!(
            try_audio_payload_types("m=audio 9 UDP/TLS/RTP/SAVPF abc\r\n"),
            Err("strconv.ParseUint: parsing \"abc\": invalid syntax".to_string())
        );
    }

    #[test]
    fn the_answer_follows_the_offerers_payload_order() {
        let a = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 9 0\r\na=rtpmap:9 G722/8000\r\n";
        assert_eq!(
            order_audio_pts(a, &[111, 0, 9, 8]),
            "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0 9\r\na=rtpmap:9 G722/8000\r\n"
        );
        assert_eq!(order_audio_pts(a, &[111, 9, 0]), a);
        // a type the offer did not list is dropped, with its attribute lines
        let b = format!("{a}a=rtcp-fb:9 transport-cc\r\na=fmtp:9 x=1\r\na=rtpmap:0 PCMU/8000\r\na=rtcp-fb:* nack\r\n");
        assert_eq!(
            order_audio_pts(&b, &[0]),
            "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\na=rtpmap:0 PCMU/8000\r\na=rtcp-fb:* nack\r\n"
        );
        assert_eq!(order_audio_pts("v=0\r\n", &[0]), "v=0\r\n");
    }

    #[test]
    fn answer_helpers() {
        let a = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 9 0\r\na=candidate:1 1 udp 1 1.2.3.4 5 typ host\r\na=candidate:2 1 udp 1 1.2.3.5 5 typ host\r\na=rtcp-mux\r\n";
        assert_eq!(audio_payload_types(a), vec![9, 0]);
        assert!(!audio_rejected(a));
        assert!(audio_rejected("m=audio 0 UDP/TLS/RTP/SAVPF 0\r\n"));
        assert!(audio_rejected("v=0\r\n"));
        let e = with_end_of_candidates(a);
        assert_eq!(
            e,
            "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 9 0\r\na=candidate:1 1 udp 1 1.2.3.4 5 typ host\r\na=candidate:2 1 udp 1 1.2.3.5 5 typ host\r\na=end-of-candidates\r\na=rtcp-mux\r\n"
        );
        assert_eq!(with_end_of_candidates(&e), e);
        assert_eq!(with_end_of_candidates("v=0\r\n"), "v=0\r\n");
    }
}
