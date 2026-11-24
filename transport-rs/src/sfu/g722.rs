//! G.722 (ITU-T SB-ADPCM, 64 kbit/s, 16 kHz in, 8 bits per sample pair out): the codec behind
//! `g722.go`. Go uses `github.com/gotranspile/g722` (a `cxgo` translation of Steve Underwood's
//! public-domain `g722_encode.c`/`g722_decode.c`, as repackaged in `dgoncharov/g722tools`, BSD).
//! This file is a hand port of that Go code, mode `Rate64000`, flags 0 (the only mode the SFU
//! uses: not packed, not 8 kHz, not ITU test mode). The arithmetic is integer, so the output is
//! bit-exact with Go by construction and is proved so by `tests/golden_sfu.rs` (vectors recorded
//! from the real library). Go's `int` is 64 bits, hence `i64` here, and the three places where
//! the Go code narrows (`int32` in `saturate`, `int16(...)`, `uint8(int8(...))`) narrow the same
//! way.
//!
//! Licence note: the algorithm tables and structure are from the BSD/public-domain sources
//! named above; this port is a derived work of that Go code and carries no extra dependency
//! (a crate would have needed a bit-exactness proof against this Go output; hand porting 300
//! lines was the shorter path).
//!
//! The Go encoder and decoder are two textually identical `block4` functions; one is used here.

const WL: [i64; 8] = [-60, -30, 58, 172, 334, 538, 1198, 3042];
const RL42: [usize; 16] = [0, 7, 6, 5, 4, 3, 2, 1, 7, 6, 5, 4, 3, 2, 1, 0];
const ILB: [i64; 32] = [
    2048, 2093, 2139, 2186, 2233, 2282, 2332, 2383, 2435, 2489, 2543, 2599, 2656, 2714, 2774, 2834,
    2896, 2960, 3025, 3091, 3158, 3228, 3298, 3371, 3444, 3520, 3597, 3676, 3756, 3838, 3922, 4008,
];
const WH: [i64; 3] = [0, -214, 798];
const RH2: [usize; 4] = [2, 1, 2, 1];
const QM2: [i64; 4] = [-7408, -1616, 7408, 1616];
const QM4: [i64; 16] = [
    0, -20456, -12896, -8968, -6288, -4240, -2584, -1200, 20456, 12896, 8968, 6288, 4240, 2584,
    1200, 0,
];
const QM6: [i64; 64] = [
    -136, -136, -136, -136, -24808, -21904, -19008, -16704, -14984, -13512, -12280, -11192, -10232,
    -9360, -8576, -7856, -7192, -6576, -6000, -5456, -4944, -4464, -4008, -3576, -3168, -2776,
    -2400, -2032, -1688, -1360, -1040, -728, 24808, 21904, 19008, 16704, 14984, 13512, 12280,
    11192, 10232, 9360, 8576, 7856, 7192, 6576, 6000, 5456, 4944, 4464, 4008, 3576, 3168, 2776,
    2400, 2032, 1688, 1360, 1040, 728, 432, 136, -432, -136,
];
const QMF_COEFFS: [i64; 12] = [3, -11, 12, 32, -210, 951, 3876, -805, 362, -156, 53, -11];
const Q6: [i64; 32] = [
    0, 35, 72, 110, 150, 190, 233, 276, 323, 370, 422, 473, 530, 587, 650, 714, 786, 858, 940,
    1023, 1121, 1219, 1339, 1458, 1612, 1765, 1980, 2195, 2557, 2919, 0, 0,
];
const ILN: [usize; 32] = [
    0, 63, 62, 31, 30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11,
    10, 9, 8, 7, 6, 5, 4, 0,
];
const ILP: [usize; 32] = [
    0, 61, 60, 59, 58, 57, 56, 55, 54, 53, 52, 51, 50, 49, 48, 47, 46, 45, 44, 43, 42, 41, 40, 39,
    38, 37, 36, 35, 34, 33, 32, 0,
];
const IHN: [usize; 3] = [0, 1, 0];
const IHP: [usize; 3] = [0, 3, 2];

/// Go's `saturate(int32)`: the value if it fits an `int16`, else the nearest bound.
fn saturate(amp: i32) -> i16 {
    let amp16 = amp as i16;
    if amp == i32::from(amp16) {
        amp16
    } else if amp > i32::from(i16::MAX) {
        i16::MAX
    } else {
        i16::MIN
    }
}

/// `int(saturate(int32(x)))`: the `int32` conversion wraps, as in Go.
fn sat(x: i64) -> i64 {
    i64::from(saturate(x as i32))
}

#[derive(Clone, Copy, Default)]
struct Band {
    s: i64,
    sp: i64,
    sz: i64,
    r: [i64; 3],
    a: [i64; 3],
    ap: [i64; 3],
    p: [i64; 3],
    d: [i64; 7],
    b: [i64; 7],
    bp: [i64; 7],
    sg: [i64; 7],
    nb: i64,
    det: i64,
}

impl Band {
    fn new(det: i64) -> Self {
        Band {
            det,
            ..Band::default()
        }
    }

    /// `encodeBlock4` / `decodeBlock4` (identical in Go): the backward-adaptive predictor update.
    fn block4(&mut self, d: i64) {
        self.d[0] = d;
        self.r[0] = sat(self.s + d);
        self.p[0] = sat(self.sz + d);
        for i in 0..3 {
            self.sg[i] = self.p[i] >> 15;
        }
        let wd1 = sat(self.a[1] << 2);
        let mut wd2 = if self.sg[0] == self.sg[1] { -wd1 } else { wd1 };
        if wd2 > i64::from(i16::MAX) {
            wd2 = i64::from(i16::MAX);
        }
        let mut wd3 = if self.sg[0] == self.sg[2] { 128 } else { -128 };
        wd3 += wd2 >> 7;
        wd3 += (self.a[2] * 32512) >> 15;
        wd3 = wd3.clamp(-12288, 12288);
        self.ap[2] = wd3;
        self.sg[0] = self.p[0] >> 15;
        self.sg[1] = self.p[1] >> 15;
        let wd1 = if self.sg[0] == self.sg[1] { 192 } else { -192 };
        let wd2 = (self.a[1] * 32640) >> 15;
        self.ap[1] = sat(wd1 + wd2);
        let wd3 = sat(15360 - self.ap[2]);
        if self.ap[1] > wd3 {
            self.ap[1] = wd3;
        } else if self.ap[1] < -wd3 {
            self.ap[1] = -wd3;
        }
        let wd1 = if d == 0 { 0 } else { 128 };
        self.sg[0] = d >> 15;
        for i in 1..7 {
            self.sg[i] = self.d[i] >> 15;
            let wd2 = if self.sg[i] == self.sg[0] { wd1 } else { -wd1 };
            let wd3 = (self.b[i] * 32640) >> 15;
            self.bp[i] = sat(wd2 + wd3);
        }
        for i in (1..7).rev() {
            self.d[i] = self.d[i - 1];
            self.b[i] = self.bp[i];
        }
        for i in (1..3).rev() {
            self.r[i] = self.r[i - 1];
            self.p[i] = self.p[i - 1];
            self.a[i] = self.ap[i];
        }
        let wd1 = sat(self.r[1] + self.r[1]);
        let wd1 = (self.a[1] * wd1) >> 15;
        let wd2 = sat(self.r[2] + self.r[2]);
        let wd2 = (self.a[2] * wd2) >> 15;
        self.sp = sat(wd1 + wd2);
        self.sz = 0;
        for i in (1..7).rev() {
            let wd1 = sat(self.d[i] + self.d[i]);
            self.sz += (self.b[i] * wd1) >> 15;
        }
        self.sz = sat(self.sz);
        self.s = sat(self.sp + self.sz);
    }

    /// The step-size scale update shared by both bands: `nb` moves by `delta`, is clamped to
    /// `0..=max`, and `det` follows (`shift_base` is 8 for the low band, 10 for the high one).
    fn scale(&mut self, delta: i64, max: i64, shift_base: i64) {
        self.nb = ((self.nb * 127) >> 7) + delta;
        self.nb = self.nb.clamp(0, max);
        let wd1 = ((self.nb >> 6) & 31) as usize;
        let wd2 = shift_base - (self.nb >> 11);
        let wd3 = if wd2 < 0 {
            ILB[wd1] << (-wd2)
        } else {
            ILB[wd1] >> wd2
        };
        self.det = wd3 << 2;
    }
}

/// Go's `g722Encoder` (state of one direction). `Default` is the lazily created state of the
/// Go zero value: `NewEncoder(Rate64000, 0)` on first use.
#[derive(Clone)]
pub struct G722Encoder {
    x: [i64; 24],
    band: [Band; 2],
}

impl Default for G722Encoder {
    fn default() -> Self {
        G722Encoder {
            x: [0; 24],
            band: [Band::new(32), Band::new(8)],
        }
    }
}

impl G722Encoder {
    pub fn new() -> Self {
        Self::default()
    }

    /// `g722Encoder.encode`: `(len+1)/2` bytes at most, one byte per pair of samples.
    ///
    /// An ODD number of samples panics (index out of range), exactly where Go's does: the
    /// encoder reads its samples in pairs. Every caller passes a whole 16 kHz frame.
    pub fn encode(&mut self, samples: &[i16]) -> Vec<u8> {
        let mut out = Vec::with_capacity(samples.len().div_ceil(2));
        let mut j = 0;
        while j < samples.len() {
            for i in 0..22 {
                self.x[i] = self.x[i + 2];
            }
            self.x[22] = i64::from(samples[j]);
            j += 1;
            self.x[23] = i64::from(samples[j]); // panics on a missing second sample, as Go does
            j += 1;
            let (mut sumeven, mut sumodd) = (0i64, 0i64);
            for i in 0..12 {
                sumodd += self.x[i * 2] * QMF_COEFFS[i];
                sumeven += self.x[i * 2 + 1] * QMF_COEFFS[11 - i];
            }
            let xlow = (sumeven + sumodd) >> 14;
            let xhigh = (sumeven - sumodd) >> 14;

            // low band: 6-bit quantiser
            let el = sat(xlow - self.band[0].s);
            let wd = if el >= 0 { el } else { -(el + 1) };
            let mut i = 1;
            while i < 30 {
                let wd1 = (Q6[i] * self.band[0].det) >> 12;
                if wd < wd1 {
                    break;
                }
                i += 1;
            }
            let ilow = if el < 0 { ILN[i] } else { ILP[i] };
            let ril = ilow >> 2;
            let dlow = (self.band[0].det * QM4[ril]) >> 15;
            self.band[0].scale(WL[RL42[ril]], 18432, 8);
            self.band[0].block4(dlow);

            // high band: 2-bit quantiser
            let eh = sat(xhigh - self.band[1].s);
            let wd = if eh >= 0 { eh } else { -(eh + 1) };
            let wd1 = (self.band[1].det * 564) >> 12;
            let mih = if wd >= wd1 { 2 } else { 1 };
            let ihigh = if eh < 0 { IHN[mih] } else { IHP[mih] };
            let dhigh = (self.band[1].det * QM2[ihigh]) >> 15;
            self.band[1].scale(WH[RH2[ihigh]], 22528, 10);
            self.band[1].block4(dhigh);

            let code = (ihigh << 6) | ilow;
            out.push(code as u8);
        }
        out
    }
}

/// Go's `g722Decoder`.
#[derive(Clone)]
pub struct G722Decoder {
    x: [i64; 24],
    band: [Band; 2],
}

impl Default for G722Decoder {
    fn default() -> Self {
        G722Decoder {
            x: [0; 24],
            band: [Band::new(32), Band::new(8)],
        }
    }
}

impl G722Decoder {
    pub fn new() -> Self {
        Self::default()
    }

    /// `g722Decoder.decode`: two samples per payload byte.
    pub fn decode(&mut self, payload: &[u8]) -> Vec<i16> {
        let mut out = Vec::with_capacity(payload.len() * 2);
        for &byte in payload {
            let code = usize::from(byte);
            let wd1 = code & 0x3F;
            let ihigh = (code >> 6) & 0x3;
            let wd2 = QM6[wd1];
            let wd1 = wd1 >> 2;
            let wd2 = (self.band[0].det * wd2) >> 15;
            let rlow = (self.band[0].s + wd2).clamp(-16384, 16383);
            let dlowt = (self.band[0].det * QM4[wd1]) >> 15;
            self.band[0].scale(WL[RL42[wd1]], 18432, 8);
            self.band[0].block4(dlowt);

            let dhigh = (self.band[1].det * QM2[ihigh]) >> 15;
            let rhigh = (dhigh + self.band[1].s).clamp(-16384, 16383);
            self.band[1].scale(WH[RH2[ihigh]], 22528, 10);
            self.band[1].block4(dhigh);

            for i in 0..22 {
                self.x[i] = self.x[i + 2];
            }
            self.x[22] = rlow + rhigh;
            self.x[23] = rlow - rhigh;
            let (mut xout1, mut xout2) = (0i64, 0i64);
            for i in 0..12 {
                xout2 += self.x[i * 2] * QMF_COEFFS[i];
                xout1 += self.x[i * 2 + 1] * QMF_COEFFS[11 - i];
            }
            out.push((xout1 >> 11) as i16);
            out.push((xout2 >> 11) as i16);
        }
        out
    }
}
