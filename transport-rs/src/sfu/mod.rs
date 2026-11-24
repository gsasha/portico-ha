//! Port of the pure audio code of the Go package `internal/sfu` (DEV-764 slice 4a): G.711 and
//! G.722, the 16 kHz <-> 8 kHz converters, the uplink buffer, the codec-selection and answer
//! policy, and the small per-session audio state machines that `sfu.go` embeds in its goroutines.
//! The WebRTC session itself (pion's `newSession`, `OnTrack`, ICE, DTLS) is `session/` in this
//! module (slice 4b: `handle.rs`, `engine.rs`, `run.rs`), on `str0m`, with `sdp.rs` and
//! `ifaces.rs` beside it.
//!
//! Go name to Rust name: `encodeMuLaw` `g711::encode_mu_law`, `decodeMuLaw`
//! `g711::decode_mu_law`, `encodeMuLawFrame`/`decodeMuLawFrame` `g711::encode_mu_law_frame`/
//! `decode_mu_law_frame`, `decodeL16Frame`/`encodeL16Frame` `resample::decode_l16_frame`/
//! `encode_l16_frame`, `downsample16to8`/`upsample8to16` `resample::downsample_16_to_8`/
//! `upsample_8_to_16`, `l16FrameToG711`/`g711FrameToL16` `resample::l16_frame_to_g711`/
//! `g711_frame_to_l16`, `g722Encoder`/`g722Decoder` `g722::G722Encoder`/`G722Decoder`,
//! `g722SilenceByte` `g722_silence_byte()`, `uplinkBuffer` `uplink::UplinkBuffer`,
//! `encodeLoop`'s conversion `audio::DownlinkEncoder`, `forwardJitterOut`'s `audio::UplinkConverter`,
//! `readRemoteTrack`'s codec gate `audio::UplinkPath`, `nextSeq`/`nextTimestamp`
//! `audio::RtpSendState`, `audioTrack.Bind` `policy::bind_codec`, the MediaEngine's answer
//! `policy::answer_codecs`. The last two are the pion MODEL (the tests and the logged cross-check
//! only): the session binds in `Core::negotiated` and sends str0m's own answer (PARITY 61).

pub mod audio;
pub mod g711;
pub mod g722;
pub mod ifaces;
pub mod policy;
pub mod resample;
pub mod sdp;
pub mod session;
pub mod uplink;

use std::sync::OnceLock;

pub use audio::{DownlinkEncoder, PhoneCodec, RtpSendState, UplinkConverter, UplinkPath};
pub use g711::{decode_mu_law, decode_mu_law_frame, encode_mu_law, encode_mu_law_frame};
pub use g722::{G722Decoder, G722Encoder};
pub use resample::{
    decode_l16_frame, downsample_16_to_8, encode_l16_frame, g711_frame_to_l16, l16_frame_to_g711,
    upsample_8_to_16,
};
pub use uplink::UplinkBuffer;

/// `g711ClockRate`.
pub const G711_CLOCK_RATE: u32 = 8000;
/// `g711PtimeMillis`.
pub const G711_PTIME_MILLIS: u32 = 40;
/// `g711SamplesPerFrame`: 320 (one 40 ms PCMU frame is 320 bytes).
pub const G711_SAMPLES_PER_FRAME: usize = (G711_CLOCK_RATE * G711_PTIME_MILLIS / 1000) as usize;
/// `g711PayloadType`: PCMU, RFC 3551 static assignment.
pub const G711_PAYLOAD_TYPE: u8 = 0;
/// `g722ClockRate`: RFC 3551 quirk, G.722 samples at 16 kHz and its RTP clock is 8 kHz.
pub const G722_CLOCK_RATE: u32 = 8000;
/// `g722PayloadType`: static assignment.
pub const G722_PAYLOAD_TYPE: u8 = 9;
/// `sip.RTPFrameBytes`: one 40 ms frame of 16 kHz L16, the link frame on both sides.
pub const L16_FRAME_BYTES: usize = crate::sip::RTP_FRAME_BYTES;

/// `g722SilenceByte`: fills a lost uplink slot. G.722 bytes are ADPCM codes whose meaning
/// depends on the decoder state, so no byte means silence; the last byte a fresh encoder
/// emits for 640 zero samples is the stand-in (computed once, as Go's package variable is).
pub fn g722_silence_byte() -> u8 {
    static B: OnceLock<u8> = OnceLock::new();
    *B.get_or_init(|| {
        let out = G722Encoder::new().encode(&[0i16; 640]);
        out[out.len() - 1]
    })
}
