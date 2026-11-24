//! `g711.go`: the mu-law (PCMU, RFC 3551 static payload type 0) encoder and decoder, the
//! reference ITU-T G.711 algorithm. A-law is not implemented in the SFU (the A-law of the
//! `gate_sip_server` driver lives in `driver::gatesipserver::media`).

const MU_LAW_BIAS: i64 = 0x84;
const MU_LAW_CLIP: i64 = 32635;
const MU_LAW_SEGMENT_END: [i64; 8] = [0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF];

/// `muLawSegment`: the first segment whose end is >= `val`, or 8.
pub fn mu_law_segment(val: i64) -> usize {
    for (i, end) in MU_LAW_SEGMENT_END.iter().enumerate() {
        if val <= *end {
            return i;
        }
    }
    MU_LAW_SEGMENT_END.len()
}

/// `encodeMuLaw`: one linear 16-bit sample to its mu-law byte.
pub fn encode_mu_law(pcm: i16) -> u8 {
    let mut v = i64::from(pcm);
    let mask: i64;
    if v < 0 {
        v = MU_LAW_BIAS - v;
        mask = 0x7F;
    } else {
        v += MU_LAW_BIAS;
        mask = 0xFF;
    }
    if v > MU_LAW_CLIP {
        v = MU_LAW_CLIP;
    }
    let seg = mu_law_segment(v);
    if seg >= 8 {
        return (0x7F ^ mask) as u8;
    }
    let uval = ((seg << 4) as i64 | ((v >> (seg + 3)) & 0x0F)) as u8;
    uval ^ (mask as u8)
}

/// `decodeMuLaw`: one mu-law byte back to a linear 16-bit sample.
pub fn decode_mu_law(u: u8) -> i16 {
    let uv = !u;
    let mut t = (i64::from(uv & 0x0F) << 3) + MU_LAW_BIAS;
    t <<= (uv & 0x70) >> 4;
    if uv & 0x80 != 0 {
        (MU_LAW_BIAS - t) as i16
    } else {
        (t - MU_LAW_BIAS) as i16
    }
}

/// `encodeMuLawFrame`: one byte per sample.
pub fn encode_mu_law_frame(samples: &[i16]) -> Vec<u8> {
    samples.iter().map(|&s| encode_mu_law(s)).collect()
}

/// `decodeMuLawFrame`: one sample per byte.
pub fn decode_mu_law_frame(mu: &[u8]) -> Vec<i16> {
    mu.iter().map(|&b| decode_mu_law(b)).collect()
}
