//! Shared by both Rust answerers (included with #[path]): G.711 mu-law, the tone generator, the
//! meter (identical in meaning to the Go peer's), the line-JSON signalling messages, and logging.
#![allow(dead_code)]
use serde_json::{json, Value};
use std::io::Write;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

pub fn ulaw_encode(pcm: i16) -> u8 {
    const BIAS: i32 = 0x84;
    const CLIP: i32 = 32635;
    let mut v = pcm as i32;
    let mut sign = 0u8;
    if v < 0 {
        v = -v;
        sign = 0x80;
    }
    if v > CLIP {
        v = CLIP;
    }
    v += BIAS;
    let mut exp = 7i32;
    let mut mask = 0x4000;
    while exp > 0 && v & mask == 0 {
        exp -= 1;
        mask >>= 1;
    }
    let mant = ((v >> (exp + 3)) & 0x0F) as u8;
    !(sign | ((exp as u8) << 4) | mant)
}

pub fn ulaw_decode(b: u8) -> i16 {
    let b = !b;
    let sign = b & 0x80;
    let exp = (b >> 4) & 7;
    let mant = (b & 0x0F) as i32;
    let s = (((mant << 3) + 0x84) << exp) - 0x84;
    if sign != 0 { -s as i16 } else { s as i16 }
}

pub struct Tone {
    hz: f64,
    phase: f64,
}
impl Tone {
    pub fn new(hz: f64) -> Self {
        Tone { hz, phase: 0.0 }
    }
    /// One 20 ms PCMU frame (160 bytes).
    pub fn frame(&mut self) -> Vec<u8> {
        let mut out = Vec::with_capacity(160);
        for _ in 0..160 {
            out.push(ulaw_encode((8000.0 * self.phase.sin()) as i16));
            self.phase += 2.0 * std::f64::consts::PI * self.hz / 8000.0;
            if self.phase > 2.0 * std::f64::consts::PI {
                self.phase -= 2.0 * std::f64::consts::PI;
            }
        }
        out
    }
}

pub struct Meter {
    hz: f64,
    first: bool,
    last_seq: u16,
    last_ts: u32,
    last_arr: Option<Instant>,
    pub rx: u64,
    lost: u64,
    late: u64,
    seq_disc: u64,
    ts_disc: u64,
    gaps: u64,
    max_gap_ms: f64,
    jitter: f64,
    max_jit: f64,
    jit_sum: f64,
    jit_n: u64,
    blk: Vec<f64>,
    blocks: i64,
    blocks_ok: i64,
    ratio_sum: f64,
    ratio_min: f64,
    rms_sum: f64,
}

impl Meter {
    pub fn new(hz: f64) -> Self {
        Meter {
            hz, first: false, last_seq: 0, last_ts: 0, last_arr: None, rx: 0, lost: 0, late: 0,
            seq_disc: 0, ts_disc: 0, gaps: 0, max_gap_ms: 0.0, jitter: 0.0, max_jit: 0.0,
            jit_sum: 0.0, jit_n: 0, blk: Vec::with_capacity(8000), blocks: 0, blocks_ok: 0,
            ratio_sum: 0.0, ratio_min: 2.0, rms_sum: 0.0,
        }
    }

    pub fn packet(&mut self, seq: u16, ts: u32, payload: &[u8], now: Instant) {
        self.rx += 1;
        if self.first {
            let d = seq.wrapping_sub(self.last_seq) as i16;
            if d == 1 {
                if ts.wrapping_sub(self.last_ts) != 160 {
                    self.ts_disc += 1;
                }
            } else if d > 1 {
                self.seq_disc += 1;
                self.lost += (d - 1) as u64;
            } else {
                self.late += 1;
            }
            let gap = now.saturating_duration_since(self.last_arr.unwrap()).as_secs_f64() * 1000.0;
            if gap > 60.0 {
                self.gaps += 1;
            }
            if gap > self.max_gap_ms {
                self.max_gap_ms = gap;
            }
            if d > 0 {
                let dd = (gap - ts.wrapping_sub(self.last_ts) as f64 / 8.0).abs();
                self.jitter += (dd - self.jitter) / 16.0;
                if self.jitter > self.max_jit {
                    self.max_jit = self.jitter;
                }
                self.jit_sum += self.jitter;
                self.jit_n += 1;
            }
            if d <= 0 {
                return;
            }
        }
        self.first = true;
        self.last_seq = seq;
        self.last_ts = ts;
        self.last_arr = Some(now);
        for &b in payload {
            self.blk.push(ulaw_decode(b) as f64);
            if self.blk.len() == 8000 {
                self.block();
            }
        }
    }

    fn block(&mut self) {
        let n = self.blk.len() as f64;
        let coeff = 2.0 * (2.0 * std::f64::consts::PI * self.hz / 8000.0).cos();
        let (mut s1, mut s2, mut e) = (0.0f64, 0.0f64, 0.0f64);
        for &x in &self.blk {
            let s0 = x + coeff * s1 - s2;
            s2 = s1;
            s1 = s0;
            e += x * x;
        }
        let p = s1 * s1 + s2 * s2 - coeff * s1 * s2;
        let ratio = if e > 0.0 { (2.0 * p / n) / e } else { 0.0 };
        self.blocks += 1;
        if self.blocks > 2 {
            self.ratio_sum += ratio;
            if ratio < self.ratio_min {
                self.ratio_min = ratio;
            }
            if ratio > 0.5 && (e / n).sqrt() > 500.0 {
                self.blocks_ok += 1;
            }
            self.rms_sum += (e / n).sqrt();
        }
        self.blk.clear();
    }

    pub fn report(&self) -> Value {
        let counted = (self.blocks - 2).max(0);
        let (mean, rms) = if counted > 0 {
            (self.ratio_sum / counted as f64, self.rms_sum / counted as f64)
        } else {
            (0.0, 0.0)
        };
        let rmin = if self.ratio_min > 1.5 { 0.0 } else { self.ratio_min };
        let jm = if self.jit_n > 0 { self.jit_sum / self.jit_n as f64 } else { 0.0 };
        let r = |f: f64| (f * 1000.0).round() / 1000.0;
        json!({
            "rx": self.rx, "lost": self.lost, "late_or_dup": self.late,
            "seq_discontinuities": self.seq_disc, "ts_discontinuities": self.ts_disc,
            "gaps_over_60ms": self.gaps, "max_gap_ms": r(self.max_gap_ms),
            "jitter_ms_mean": r(jm), "jitter_ms_max": r(self.max_jit),
            "tone_blocks": counted, "tone_blocks_ok": self.blocks_ok, "tone_ratio_mean": r(mean),
            "tone_ratio_min": r(rmin), "rms_mean": r(rms),
            "tone_detected": counted > 0 && self.blocks_ok as f64 >= 0.9 * counted as f64,
        })
    }
}

pub fn now_hms() -> String {
    let d = SystemTime::now().duration_since(UNIX_EPOCH).unwrap();
    let s = d.as_secs() % 86400;
    format!("{:02}:{:02}:{:02}.{:03}", s / 3600, (s / 60) % 60, s % 60, d.subsec_millis())
}

pub fn logj(mut v: Value) {
    v["ts"] = Value::String(now_hms());
    let mut out = std::io::stdout().lock();
    let _ = writeln!(out, "{}", v);
    let _ = out.flush();
}

/// Strip the a=candidate lines from an SDP and return (sdp without them, the candidate strings).
pub fn split_candidates(sdp: &str) -> (String, Vec<String>) {
    let mut keep = String::new();
    let mut cands = vec![];
    for line in sdp.split("\r\n") {
        if line.is_empty() {
            continue;
        }
        if let Some(c) = line.strip_prefix("a=candidate:") {
            cands.push(format!("candidate:{c}"));
        } else if line == "a=end-of-candidates" {
        } else {
            keep.push_str(line);
            keep.push_str("\r\n");
        }
    }
    (keep, cands)
}

#[derive(Clone)]
pub struct Args {
    pub listen: String,
    pub sessions: usize,
    pub hz: f64,
    pub rx_hz: f64,
    pub snap: f64,
    pub trickle: bool,
}
impl Args {
    pub fn parse() -> Args {
        let mut a = Args { listen: "127.0.0.1:0".into(), sessions: 1, hz: 440.0, rx_hz: 440.0, snap: 10.0, trickle: true };
        let v: Vec<String> = std::env::args().collect();
        let mut i = 1;
        while i < v.len() {
            let val = v.get(i + 1).cloned().unwrap_or_default();
            match v[i].as_str() {
                "--listen" => a.listen = val,
                "--sessions" => a.sessions = val.parse().unwrap(),
                "--hz" => a.hz = val.parse().unwrap(),
                "--rx-hz" => a.rx_hz = val.parse().unwrap(),
                "--snap" => a.snap = val.parse().unwrap(),
                "--no-trickle" => { a.trickle = false; i -= 1; }
                x => panic!("unknown flag {x}"),
            }
            i += 2;
        }
        a
    }
}
