//! `resample.go`: the sample-rate half of the L16 <-> G.711 conversion. The link to the device is
//! 16 kHz little-endian L16; G.711 is 8 kHz. Box-filter decimation down, sample duplication up,
//! deliberately simple (Go's own doc comment flags the aliasing).

/// `decodeL16Frame`: little-endian L16 bytes to samples (a trailing odd byte is ignored).
pub fn decode_l16_frame(frame: &[u8]) -> Vec<i16> {
    frame
        .as_chunks::<2>()
        .0
        .iter()
        .map(|c| i16::from_le_bytes(*c))
        .collect()
}

/// `encodeL16Frame`: samples to little-endian L16 bytes.
pub fn encode_l16_frame(samples: &[i16]) -> Vec<u8> {
    let mut out = Vec::with_capacity(samples.len() * 2);
    for s in samples {
        out.extend_from_slice(&s.to_le_bytes());
    }
    out
}

/// `downsample16to8`: the average of each adjacent pair, truncated toward zero (Go's `/`); an
/// odd trailing sample is dropped.
pub fn downsample_16_to_8(samples: &[i16]) -> Vec<i16> {
    samples
        .as_chunks::<2>()
        .0
        .iter()
        .map(|p| ((i32::from(p[0]) + i32::from(p[1])) / 2) as i16)
        .collect()
}

/// `upsample8to16`: every sample twice.
pub fn upsample_8_to_16(samples: &[i16]) -> Vec<i16> {
    let mut out = Vec::with_capacity(samples.len() * 2);
    for &s in samples {
        out.push(s);
        out.push(s);
    }
    out
}

/// `l16FrameToG711`: the phone-bound conversion for a PCMU phone: decode, downsample, mu-law.
pub fn l16_frame_to_g711(frame: &[u8]) -> Vec<u8> {
    super::g711::encode_mu_law_frame(&downsample_16_to_8(&decode_l16_frame(frame)))
}

/// `g711FrameToL16`: the device-bound conversion for a PCMU phone: mu-law decode, upsample,
/// L16 bytes.
pub fn g711_frame_to_l16(g711: &[u8]) -> Vec<u8> {
    encode_l16_frame(&upsample_8_to_16(&super::g711::decode_mu_law_frame(g711)))
}
