//! The audio logic `sfu.go` keeps inside the goroutines of a `Session`, lifted out so slice 4b's
//! session can call it and the tests can drive it without a WebRTC stack:
//!
//! * `DownlinkEncoder`: `encodeLoop`'s choice of conversion (G.722 or PCMU) for a device frame;
//! * `RtpSendState`: `nextSeq` / `nextTimestamp` and the SSRC;
//! * `UplinkPath`: `readRemoteTrack`'s gate (only the two codecs we decode, only the FIRST one
//!   seen) in front of the jitter buffer, with `SetSilence` for G.722;
//! * `UplinkConverter`: `forwardJitterOut`'s conversion of a played-out payload to a link frame.

use std::sync::atomic::{AtomicI32, AtomicU64, Ordering};
use std::sync::Arc;

use super::resample::{decode_l16_frame, encode_l16_frame, g711_frame_to_l16, l16_frame_to_g711};
use super::uplink::UplinkBuffer;
use super::{g722_silence_byte, G722Decoder, G722Encoder, G711_PAYLOAD_TYPE, G722_PAYLOAD_TYPE};

/// The two codecs the phone leg speaks.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PhoneCodec {
    /// PCMU, payload type 0.
    Pcmu,
    /// G.722, payload type 9.
    G722,
}

impl PhoneCodec {
    /// The static payload type `sfu.go` compares against.
    pub fn payload_type(self) -> u8 {
        match self {
            PhoneCodec::Pcmu => G711_PAYLOAD_TYPE,
            PhoneCodec::G722 => G722_PAYLOAD_TYPE,
        }
    }

    pub fn from_payload_type(pt: u8) -> Option<Self> {
        match pt {
            G711_PAYLOAD_TYPE => Some(PhoneCodec::Pcmu),
            G722_PAYLOAD_TYPE => Some(PhoneCodec::G722),
            _ => None,
        }
    }
}

/// Device frame (16 kHz L16) to phone payload: `encodeLoop`'s branch. One G.722 encoder state per
/// session, used from one task only.
#[derive(Default)]
pub struct DownlinkEncoder {
    enc: G722Encoder,
}

impl DownlinkEncoder {
    pub fn new() -> Self {
        Self::default()
    }

    /// The payload for `frame` when the phone is sent `codec`. `None` for a G.722 frame with an
    /// odd number of samples: Go's encoder indexes past the end there, and a panic in an
    /// `encodeLoop` goroutine kills the whole Go process, so the Rust port drops such a frame
    /// instead (deliberate difference; the hub sends 1280-byte frames, which are never odd).
    pub fn encode(&mut self, codec: PhoneCodec, frame: &[u8]) -> Option<Vec<u8>> {
        match codec {
            PhoneCodec::G722 => {
                let samples = decode_l16_frame(frame);
                if !samples.len().is_multiple_of(2) {
                    return None;
                }
                Some(self.enc.encode(&samples))
            }
            PhoneCodec::Pcmu => Some(l16_frame_to_g711(frame)),
        }
    }
}

/// `Session.seq`, `ts`, `ssrc` and their two accessors.
#[derive(Debug)]
pub struct RtpSendState {
    seq: u16,
    ts: u32,
    ssrc: u32,
}

impl RtpSendState {
    /// A fresh session: sequence 0, timestamp 0. (No random-SSRC constructor: the session's
    /// SSRC is str0m's own, and R1-164 took the OS random source out of the session.)
    pub fn with_ssrc(ssrc: u32) -> Self {
        RtpSendState {
            seq: 0,
            ts: 0,
            ssrc,
        }
    }

    pub fn ssrc(&self) -> u32 {
        self.ssrc
    }

    /// `nextSeq`: the current sequence number, then increments (wrapping).
    pub fn next_seq(&mut self) -> u16 {
        let s = self.seq;
        self.seq = self.seq.wrapping_add(1);
        s
    }

    /// `nextTimestamp(ticks)`: the current timestamp, then advances it by the frame's payload
    /// bytes (both codecs run an 8000 Hz RTP clock and carry one byte per tick).
    pub fn next_timestamp(&mut self, ticks: u32) -> u32 {
        let t = self.ts;
        self.ts = self.ts.wrapping_add(ticks);
        t
    }
}

/// What `UplinkGate::admit` decided about a packet's payload type.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Admit {
    /// The first accepted packet: it fixes the call's codec.
    First,
    /// Same codec as the first.
    Accepted,
    /// Not PCMU/G.722, or not the codec of the first packet: dropped and counted.
    Foreign,
}

/// `Session.upCodec` (`atomic.Int32`, -1 until the first packet), plus the payload types the
/// session NEGOTIATED. Go compares the packet's wire payload type with the static 0 and 9; here
/// the two types are what the str0m answer named (`set_negotiated`), defaulting to the static
/// ones until a session sets them (browsers use the static ones, so nothing changes for them; a
/// peer that offered G.722/PCMU under dynamic types is no longer dropped as foreign).
#[derive(Debug)]
pub struct UplinkGate {
    /// -1 until the first admitted packet, then 0 (PCMU) or 1 (G.722).
    codec: AtomicI32,
    pcmu_pt: AtomicI32,
    g722_pt: AtomicI32,
}

impl Default for UplinkGate {
    fn default() -> Self {
        UplinkGate {
            codec: AtomicI32::new(-1),
            pcmu_pt: AtomicI32::new(i32::from(G711_PAYLOAD_TYPE)),
            g722_pt: AtomicI32::new(i32::from(G722_PAYLOAD_TYPE)),
        }
    }
}

impl UplinkGate {
    /// The wire payload types of PCMU and G.722 as negotiated (`None` = not negotiated, every
    /// packet of that codec is foreign).
    pub fn set_negotiated(&self, pcmu: Option<u8>, g722: Option<u8>) {
        self.pcmu_pt
            .store(pcmu.map_or(-1, i32::from), Ordering::SeqCst);
        self.g722_pt
            .store(g722.map_or(-1, i32::from), Ordering::SeqCst);
    }

    fn lookup(&self, pt: u8) -> Option<PhoneCodec> {
        let pt = i32::from(pt);
        if pt == self.pcmu_pt.load(Ordering::SeqCst) {
            Some(PhoneCodec::Pcmu)
        } else if pt == self.g722_pt.load(Ordering::SeqCst) {
            Some(PhoneCodec::G722)
        } else {
            None
        }
    }

    /// The phone's codec, once its first packet has been admitted.
    pub fn codec(&self) -> Option<PhoneCodec> {
        match self.codec.load(Ordering::SeqCst) {
            0 => Some(PhoneCodec::Pcmu),
            1 => Some(PhoneCodec::G722),
            _ => None,
        }
    }

    /// `readRemoteTrack`'s test, in Go's order: a payload type that is neither negotiated codec
    /// is foreign; the first one seen wins (`CompareAndSwap(-1, ...)`); a later packet of the
    /// other codec is foreign.
    pub fn admit(&self, pt: u8) -> Admit {
        let Some(c) = self.lookup(pt) else {
            return Admit::Foreign;
        };
        let code = match c {
            PhoneCodec::Pcmu => 0,
            PhoneCodec::G722 => 1,
        };
        if self
            .codec
            .compare_exchange(-1, code, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
        {
            Admit::First
        } else if self.codec.load(Ordering::SeqCst) != code {
            Admit::Foreign
        } else {
            Admit::Accepted
        }
    }
}

/// `readRemoteTrack`'s per-packet work: the codec gate, the G.722 silence byte, the jitter push.
/// The caller (the session task, `sfu/session/`) logs the first packet and, at the end of the stream, the
/// `foreign()` count, as Go does.
pub struct UplinkPath {
    gate: Arc<UplinkGate>,
    jitter: UplinkBuffer,
    foreign: AtomicU64,
}

impl UplinkPath {
    pub fn new(jitter: UplinkBuffer) -> Self {
        UplinkPath {
            gate: Arc::new(UplinkGate::default()),
            jitter,
            foreign: AtomicU64::new(0),
        }
    }

    /// The gate, shared with the `UplinkConverter` of the forwarding task.
    pub fn gate(&self) -> Arc<UplinkGate> {
        self.gate.clone()
    }

    pub fn jitter(&self) -> &UplinkBuffer {
        &self.jitter
    }

    /// Packets dropped as foreign so far.
    pub fn foreign(&self) -> u64 {
        self.foreign.load(Ordering::SeqCst)
    }

    /// One received RTP packet. Returns what the gate decided. Must run inside a tokio runtime
    /// (the first accepted packet starts the jitter buffer).
    pub fn on_rtp(&self, pt: u8, seq: u16, payload: Vec<u8>) -> Admit {
        let a = self.gate.admit(pt);
        match a {
            Admit::Foreign => {
                self.foreign.fetch_add(1, Ordering::SeqCst);
                return a;
            }
            Admit::First if self.gate.codec() == Some(PhoneCodec::G722) => {
                self.jitter.set_silence(g722_silence_byte());
            }
            _ => {}
        }
        self.jitter.push(seq, payload);
        a
    }
}

/// Gain steps of a concealed run of lost slots: the first repeats the last good audio at 70 %,
/// then 40 %, then 15 %, then silence (a longer repeat is a buzz, not speech).
const CONCEAL_GAINS: [f32; 3] = [0.70, 0.40, 0.15];

/// `forwardJitterOut`'s conversion: a payload the jitter buffer played out to a 16 kHz L16 link
/// frame. G.722 is already 16 kHz, nothing to resample; everything else is PCMU. One G.722
/// decoder state per session, used from the forwarding task only.
///
/// A LOST slot (`convert_slot(.., true)`) is never decoded. The jitter buffer fills it with a
/// stand-in byte, and for G.722 any byte fed to the decoder is an ADPCM code that moves its
/// predictor and step size, so the stand-in garbled the next real frames (the "progressively
/// garbled" tablet calls). The slot is concealed instead: the tail of the last good output
/// frame is repeated, fading out over a few slots, and the decoder is left untouched.
pub struct UplinkConverter {
    gate: Arc<UplinkGate>,
    dec: G722Decoder,
    /// the last good output frame (samples), the source of the concealment
    last: Vec<i16>,
    /// consecutive lost slots so far
    run: usize,
    concealed: u64,
    decoder_resets: u64,
}

impl UplinkConverter {
    pub fn new(gate: Arc<UplinkGate>) -> Self {
        UplinkConverter {
            gate,
            dec: G722Decoder::new(),
            last: Vec::new(),
            run: 0,
            concealed: 0,
            decoder_resets: 0,
        }
    }

    /// A payload that really arrived.
    pub fn convert(&mut self, payload: &[u8]) -> Vec<u8> {
        self.convert_slot(payload, false)
    }

    /// A payload, or (`lost`) the stand-in for a slot that never arrived.
    pub fn convert_slot(&mut self, payload: &[u8], lost: bool) -> Vec<u8> {
        // only G.722 conceals: its decoder is stateful, so the stand-in byte did damage. PCMU
        // decodes each byte on its own, and its stand-in (0xFF) is exact silence.
        if lost && self.gate.codec() == Some(PhoneCodec::G722) {
            return encode_l16_frame(&self.conceal(payload.len() * 2));
        }
        self.run = 0;
        let samples = if self.gate.codec() == Some(PhoneCodec::G722) {
            self.dec.decode(payload)
        } else {
            decode_l16_frame(&g711_frame_to_l16(payload))
        };
        self.last.clear();
        self.last.extend_from_slice(&samples);
        encode_l16_frame(&samples)
    }

    /// `n` samples standing in for a lost slot.
    fn conceal(&mut self, n: usize) -> Vec<i16> {
        self.concealed += 1;
        let step = self.run;
        self.run += 1;
        let from = CONCEAL_GAINS.get(step.wrapping_sub(1)).copied().unwrap_or(0.0);
        let from = if step == 0 { 1.0 } else { from };
        let to = CONCEAL_GAINS.get(step).copied().unwrap_or(0.0);
        if self.last.is_empty() || to == 0.0 && from == 0.0 {
            return vec![0; n];
        }
        // the last `n` good samples (tiled when the last frame was shorter), the gain ramping
        // from the previous slot's level to this one's so the fade has no step
        let l = self.last.len();
        let m = l.min(n);
        (0..n)
            .map(|i| {
                let g = from + (to - from) * (i as f32 / n as f32);
                (f32::from(self.last[l - m + i % m]) * g) as i16
            })
            .collect()
    }

    /// The sender restarted its stream (the jitter buffer resynchronised): the decoder state
    /// belongs to the old stream, so start it afresh.
    pub fn reset_decoder(&mut self) {
        self.dec = G722Decoder::new();
        self.last.clear();
        self.run = 0;
        self.decoder_resets += 1;
    }

    /// Slots concealed so far.
    pub fn concealed(&self) -> u64 {
        self.concealed
    }

    /// Decoder restarts so far.
    pub fn decoder_resets(&self) -> u64 {
        self.decoder_resets
    }
}
