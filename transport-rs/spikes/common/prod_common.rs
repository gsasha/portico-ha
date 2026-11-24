//! Shared by the two production-shaped answerers of spike S1b (included with #[path]): logging with epoch
//! milliseconds, local interface enumeration, the offer rewrite, the offer limits and the spectrum meter.
#![allow(dead_code)]
use crate::common::*;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::Write;
use std::net::IpAddr;
use std::time::{SystemTime, UNIX_EPOCH};

// Production limits on the offer text (a browser offer is 1 to 6 KB and 30 to 120 lines; these are generous).
// `--no-sdp-limits` switches them off to measure str0m's raw behaviour on hostile input.
pub const MAX_SDP_BYTES: usize = 64 * 1024;
pub const MAX_SDP_LINES: usize = 1000;
pub fn t_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64
}

pub fn log(mut v: Value) {
    v["t_ms"] = json!(t_ms());
    let mut out = std::io::stdout().lock();
    let _ = writeln!(out, "{v}");
    let _ = out.flush();
}

/// The local addresses to gather host candidates for: every up interface address, IPv4 and IPv6, loopback
/// included. IPv6 link-local (fe80::/10) is skipped (it needs a scope id and browsers do not offer it).
pub fn local_addrs(only_loopback: bool) -> Vec<(String, IpAddr)> {
    let mut v = vec![];
    for i in if_addrs::get_if_addrs().unwrap_or_default() {
        let ip = i.ip();
        if let IpAddr::V6(a) = ip {
            if (a.segments()[0] & 0xffc0) == 0xfe80 {
                continue;
            }
        }
        if only_loopback && !ip.is_loopback() {
            continue;
        }
        v.push((i.name.clone(), ip));
    }
    v
}

/// Rewrite for str0m 0.25: `a=rtpmap:<pt> <name>/<rate>/1` becomes `a=rtpmap:<pt> <name>/<rate>` (only the
/// trailing `/1` of an rtpmap line; `opus/48000/2` and friends are untouched). Returns the new text.
pub fn normalise_offer(sdp: &str) -> String {
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

/// 1 s Goertzel scan: which frequency dominates the decoded mu-law.
pub struct Spectrum {
    blk: Vec<f64>,
    hist: HashMap<u32, u32>,
    silent: u32,
    blocks: u32,
}
impl Spectrum {
    pub fn new() -> Self {
        Spectrum { blk: Vec::with_capacity(8000), hist: HashMap::new(), silent: 0, blocks: 0 }
    }
    pub fn feed(&mut self, payload: &[u8]) {
        for &b in payload {
            self.blk.push(ulaw_decode(b) as f64);
            if self.blk.len() == 8000 {
                self.block();
            }
        }
    }
    fn block(&mut self) {
        self.blocks += 1;
        let e: f64 = self.blk.iter().map(|x| x * x).sum::<f64>() / 8000.0;
        if e.sqrt() < 30.0 {
            self.silent += 1;
            self.blk.clear();
            return;
        }
        let (mut best, mut bp) = (0u32, 0.0f64);
        let mut f = 100u32;
        while f <= 3900 {
            let coeff = 2.0 * (2.0 * std::f64::consts::PI * f as f64 / 8000.0).cos();
            let (mut s1, mut s2) = (0.0f64, 0.0f64);
            for &x in &self.blk {
                let s0 = x + coeff * s1 - s2;
                s2 = s1;
                s1 = s0;
            }
            let p = s1 * s1 + s2 * s2 - coeff * s1 * s2;
            if p > bp {
                bp = p;
                best = f;
            }
            f += 20;
        }
        *self.hist.entry(best).or_insert(0) += 1;
        self.blk.clear();
    }
    pub fn report(&self) -> Value {
        let mut h: Vec<_> = self.hist.iter().collect();
        h.sort_by(|a, b| b.1.cmp(a.1));
        json!({"blocks": self.blocks, "silent_blocks": self.silent,
               "dominant_hz_top": h.iter().take(3).map(|(f, c)| json!([f, c])).collect::<Vec<_>>()})
    }
}


/// True when the offer text is beyond what a browser ever sends (see MAX_SDP_*).
pub fn offer_too_large(sdp: &str) -> bool {
    sdp.len() > MAX_SDP_BYTES || sdp.matches('\n').count() > MAX_SDP_LINES
}
