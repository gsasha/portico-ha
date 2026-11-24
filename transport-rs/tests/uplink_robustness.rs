//! The 2026-11-21 tablet-call fixes on the uplink (phone to station): a jitter delay that absorbs
//! a Wi-Fi burst, and a lost G.722 slot that does not corrupt the decoder.

use std::f64::consts::PI;
use std::time::Duration;

use tokio::time::sleep;
use transport_rs::sfu::{
    decode_l16_frame, g722_silence_byte, G722Decoder, G722Encoder, PhoneCodec, UplinkBuffer,
    UplinkConverter, UplinkPath, G722_PAYLOAD_TYPE,
};
use transport_rs::sip::JitterBuffer;

const MS: fn(u64) -> Duration = Duration::from_millis;

/// `n` 20 ms G.722 frames (160 bytes) of a 300 Hz tone, from one encoder.
fn tone_frames(n: usize) -> Vec<Vec<u8>> {
    let mut enc = G722Encoder::new();
    (0..n)
        .map(|k| {
            let s: Vec<i16> = (0..320)
                .map(|i| ((((k * 320 + i) as f64) * 2.0 * PI * 300.0 / 16000.0).sin() * 8000.0) as i16)
                .collect();
            enc.encode(&s)
        })
        .collect()
}

/// A phone whose Wi-Fi holds packets 10..16 back and delivers them together when packet 16 is
/// sent, so the oldest is 120 ms late, the shape of the kitchen tablet's calls; returns the slots
/// played as silence.
async fn lost_slots(burst: bool, push: impl Fn(u16, Vec<u8>), stats: impl Fn() -> u64) -> u64 {
    let frames = tone_frames(120);
    for (k, f) in frames.iter().enumerate() {
        if burst && k == 16 {
            for j in 10..16u16 {
                push(j, frames[j as usize].clone()); // the held packets arrive in one burst
            }
        }
        if !(burst && (10..16).contains(&k)) {
            push(k as u16, f.clone());
        }
        sleep(MS(20)).await;
    }
    sleep(MS(500)).await;
    stats()
}

#[tokio::test(start_paused = true)]
async fn the_uplink_delay_absorbs_a_120_ms_burst_that_60_ms_could_not() {
    // (the buffer goes on playing silence after the stream ends, so the count is compared with
    // the same stream without the burst)
    let run_old = |burst| async move {
        let old = JitterBuffer::new(MS(20), MS(60), 160, g722_silence_byte());
        let o = old.clone();
        let n = lost_slots(burst, move |s, p| old.push(s, p), move || o.stats().silence_slots).await;
        n
    };
    let run_new = |burst| async move {
        let ub = UplinkBuffer::new();
        let u = ub.clone();
        lost_slots(burst, move |s, p| ub.push(s, p), move || u.jitter_stats().silence_slots).await
    };
    // before: a 60 ms delay (what Go and the first port used) loses the burst
    let (old_flat, old_burst) = (run_old(false).await, run_old(true).await);
    assert!(old_burst >= old_flat + 2, "the 60 ms buffer should have lost the burst: {old_flat} vs {old_burst}");
    // after: the production buffer plays every slot of the burst
    let (new_flat, new_burst) = (run_new(false).await, run_new(true).await);
    assert_eq!(new_burst, new_flat, "the 140 ms uplink buffer must play every slot");
}

#[tokio::test(start_paused = true)]
async fn the_resync_window_follows_the_longer_delay() {
    // 140 ms / 20 ms ptime: 3 x 7 = 21 frames of ordinary jitter before a packet is an outlier
    let ub = UplinkBuffer::new();
    ub.push(0, vec![1; 160]);
    sleep(MS(300)).await;
    // ~17 ahead of next_seq is still ordinary (it is stored, no outlier counted)...
    ub.push(25, vec![2; 160]);
    assert_eq!(ub.jitter_stats().outliers_refused, 0);
    // ...and far ahead is refused as a possible restart
    ub.push(200, vec![3; 160]);
    assert_eq!(ub.jitter_stats().outliers_refused, 1);
    ub.close();
}

#[test]
fn a_lost_g722_slot_leaves_the_decoder_state_alone() {
    let frames = tone_frames(6);
    let gate_conv = || {
        let path = UplinkPath::new(UplinkBuffer::new());
        (path.gate(), path)
    };
    // the converter needs a G.722 gate: admit one packet (needs a runtime for the buffer)
    let rt = tokio::runtime::Builder::new_current_thread().enable_all().start_paused(true).build().unwrap();
    rt.block_on(async {
        let (g1, p1) = gate_conv();
        let (g2, p2) = gate_conv();
        let (g3, p3) = gate_conv();
        for p in [&p1, &p2, &p3] {
            p.on_rtp(G722_PAYLOAD_TYPE, 0, vec![0; 160]);
        }
        let mut gap = UplinkConverter::new(g1); // 3 real, one lost, 2 real
        let mut clean = UplinkConverter::new(g2); // the same real frames, the slot just absent
        let mut stuffed = UplinkConverter::new(g3); // the OLD behaviour: the stand-in byte decoded
        let mut after_gap = Vec::new();
        let mut after_clean = Vec::new();
        let mut after_stuffed = Vec::new();
        for (i, f) in frames.iter().enumerate().take(3) {
            gap.convert_slot(f, false);
            clean.convert(f);
            stuffed.convert(f);
            let _ = i;
        }
        let silence = vec![g722_silence_byte(); 160];
        let c = gap.convert_slot(&silence, true);
        assert_eq!(c.len(), 640, "a 20 ms slot is 320 samples of L16");
        stuffed.convert(&silence);
        for f in &frames[3..5] {
            after_gap.push(gap.convert_slot(f, false));
            after_clean.push(clean.convert(f));
            after_stuffed.push(stuffed.convert(f));
        }
        assert_eq!(after_gap, after_clean, "concealing must not move the decoder");
        assert_ne!(after_stuffed, after_clean, "the stand-in byte did move it (the bug)");
        assert_eq!(gap.concealed(), 1);
        for p in [p1, p2, p3] {
            p.jitter().close();
        }
    });
}

#[test]
fn concealment_repeats_the_last_audio_fading_to_silence() {
    let frames = tone_frames(4);
    let rt = tokio::runtime::Builder::new_current_thread().enable_all().start_paused(true).build().unwrap();
    rt.block_on(async {
        let path = UplinkPath::new(UplinkBuffer::new());
        path.on_rtp(G722_PAYLOAD_TYPE, 0, vec![0; 160]);
        assert_eq!(path.gate().codec(), Some(PhoneCodec::G722));
        let mut c = UplinkConverter::new(path.gate());
        let mut last = Vec::new();
        for f in &frames {
            last = decode_l16_frame(&c.convert(f));
        }
        let silence = vec![g722_silence_byte(); 160];
        let rms = |s: &[i16]| (s.iter().map(|v| f64::from(*v).powi(2)).sum::<f64>() / s.len() as f64).sqrt();
        let base = rms(&last);
        assert!(base > 3000.0, "the tone is there: {base}");
        let mut levels = Vec::new();
        for _ in 0..6 {
            levels.push(rms(&decode_l16_frame(&c.convert_slot(&silence, true))));
        }
        // loud at first (it is the last real audio), strictly quieter with each lost slot, then silence
        assert!(levels[0] > 0.6 * base && levels[0] < base, "{levels:?}");
        assert!(levels[0] > levels[1] && levels[1] > levels[2] && levels[2] > levels[3], "{levels:?}");
        assert_eq!(levels[4], 0.0, "{levels:?}");
        assert_eq!(levels[5], 0.0, "{levels:?}");
        // a real frame after the gap is decoded normally again
        let again = decode_l16_frame(&c.convert(&frames[0]));
        assert!(rms(&again) > 1000.0);
        // a sequence restart resets the decoder and is counted
        c.reset_decoder();
        assert_eq!(c.decoder_resets(), 1);
        let fresh = G722Decoder::new().decode(&frames[0]);
        assert_eq!(decode_l16_frame(&c.convert(&frames[0])), fresh, "a reset decoder decodes like a new one");
        path.jitter().close();
    });
}
