//! The ported Go tests of `internal/sfu` that need no pion session (g711_test.go, codec_test.go,
//! tone_test.go, wideband_test.go and the pion-free parts of sfu_test.go), plus the speed check.
//! The Go name is in a comment on each test. Where Go's test drives a real WebRTC pair, the
//! audio assertions are kept and run through the Rust audio path directly (`UplinkPath` ->
//! `UplinkConverter`, `DownlinkEncoder`); the WebRTC hop itself is a slice 4b test (PARITY.md,
//! "slice 4b tests").

use std::f64::consts::PI;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use tokio::time::{interval, sleep, timeout};
use transport_rs::sfu::policy::{answer_codecs, bind_codec};
use transport_rs::sfu::{
    decode_l16_frame, decode_mu_law, decode_mu_law_frame, downsample_16_to_8, encode_l16_frame,
    encode_mu_law, encode_mu_law_frame, g711_frame_to_l16, g722_silence_byte, l16_frame_to_g711,
    upsample_8_to_16, DownlinkEncoder, G722Decoder, G722Encoder, PhoneCodec, UplinkBuffer,
    UplinkConverter, UplinkPath, G711_PAYLOAD_TYPE, G711_SAMPLES_PER_FRAME, G722_PAYLOAD_TYPE,
};

// ---- helpers of tone_test.go / wideband_test.go ------------------------------------------------

/// `toneFreq`: a tone's frequency from its positive-going zero crossings over the whole slice.
fn tone_freq(s: &[i16], rate: f64) -> f64 {
    let (mut first, mut last, mut n) = (None, 0usize, 0u32);
    for i in 1..s.len() {
        if s[i - 1] < 0 && s[i] >= 0 {
            first.get_or_insert(i);
            last = i;
            n += 1;
        }
    }
    if n < 2 {
        return 0.0;
    }
    f64::from(n - 1) * rate / (last - first.unwrap()) as f64
}

fn peak(s: &[i16]) -> i32 {
    s.iter().map(|v| i32::from(*v).abs()).max().unwrap_or(0)
}

/// `longestRunBelow`: the longest stretch of consecutive samples whose magnitude is under `thr`.
fn longest_run_below(s: &[i16], thr: i32) -> usize {
    let (mut best, mut cur) = (0, 0);
    for v in s {
        if i32::from(*v).abs() < thr {
            cur += 1;
            best = best.max(cur);
        } else {
            cur = 0;
        }
    }
    best
}

fn rms(s: &[i16]) -> f64 {
    let e: f64 = s.iter().map(|v| f64::from(*v) * f64::from(*v)).sum();
    (e / s.len() as f64).sqrt()
}

/// `tone16k`.
fn tone16k(n: usize, hz: f64, amp: f64, phase: &mut f64) -> Vec<i16> {
    (0..n)
        .map(|_| {
            let v = (amp * phase.sin()) as i16;
            *phase += 2.0 * PI * hz / 16000.0;
            v
        })
        .collect()
}

// ---- g711_test.go -----------------------------------------------------------------------------

// TestMuLawRoundTrip
#[test]
fn mu_law_round_trip() {
    let cases: [(i16, u8, i16); 13] = [
        (0, 255, 0),
        (100, 242, 104),
        (-100, 114, -104),
        (1000, 206, 988),
        (-1000, 78, -988),
        (8000, 160, 7932),
        (-8000, 32, -7932),
        (20000, 140, 19836),
        (-20000, 12, -19836),
        (32767, 128, 32124),
        (-32768, 0, -32124),
        (5000, 171, 5116),
        (-5000, 43, -5116),
    ];
    for (linear, want_encoded, want_decoded) in cases {
        let enc = encode_mu_law(linear);
        assert_eq!(enc, want_encoded, "encodeMuLaw({linear})");
        assert_eq!(
            decode_mu_law(enc),
            want_decoded,
            "decodeMuLaw(encodeMuLaw({linear}))"
        );
    }
}

// TestMuLawFrameRoundTrip
#[test]
fn mu_law_frame_round_trip() {
    let samples: Vec<i16> = (0..G711_SAMPLES_PER_FRAME as i64)
        .map(|i| ((i * 137) % 65536 - 32768) as i16)
        .collect();
    let encoded = encode_mu_law_frame(&samples);
    assert_eq!(encoded.len(), G711_SAMPLES_PER_FRAME);
    let decoded = decode_mu_law_frame(&encoded);
    assert_eq!(decoded.len(), G711_SAMPLES_PER_FRAME);
    for (i, (d, o)) in decoded.iter().zip(&samples).enumerate() {
        let diff = (i32::from(*d) - i32::from(*o)).abs();
        let tolerance = 300 + i32::from(*o).abs() / 8;
        assert!(diff <= tolerance, "sample {i}: {d} vs {o}");
    }
}

// TestL16G711FrameRoundTrip
#[test]
fn l16_g711_frame_round_trip() {
    const L16_FRAME_BYTES: usize = 1280;
    let samples = vec![4000i16; L16_FRAME_BYTES / 2];
    let frame = encode_l16_frame(&samples);
    assert_eq!(frame.len(), L16_FRAME_BYTES);
    let g711 = l16_frame_to_g711(&frame);
    assert_eq!(g711.len(), G711_SAMPLES_PER_FRAME);
    let back = g711_frame_to_l16(&g711);
    assert_eq!(back.len(), L16_FRAME_BYTES);
    for (i, s) in decode_l16_frame(&back).iter().enumerate() {
        assert!((i32::from(*s) - 4000).abs() <= 400, "sample {i} = {s}");
    }
}

// TestDownsampleUpsampleShapes
#[test]
fn downsample_upsample_shapes() {
    let down = downsample_16_to_8(&[0i16; 640]);
    assert_eq!(down.len(), 320);
    assert_eq!(upsample_8_to_16(&down).len(), 640);
}

// TestDownsampleAverages
#[test]
fn downsample_averages() {
    let down = downsample_16_to_8(&[100, 200, -50, -50]);
    assert_eq!(down[0], 150);
    assert_eq!(down[1], -50);
}

// TestUpsampleDuplicates
#[test]
fn upsample_duplicates() {
    assert_eq!(upsample_8_to_16(&[7, -9]), vec![7, 7, -9, -9]);
}

// TestL16FrameCodec
#[test]
fn l16_frame_codec() {
    let samples = [0i16, 1, -1, 32767, -32768, 12345, -12345];
    let frame = encode_l16_frame(&samples);
    assert_eq!(frame.len(), samples.len() * 2);
    assert_eq!(decode_l16_frame(&frame), samples);
}

// ---- sfu_test.go (no pion) ---------------------------------------------------------------------

// TestL16FrameToG711FrameShapes
#[test]
fn l16_frame_to_g711_frame_shapes() {
    let g711 = l16_frame_to_g711(&[0u8; 1280]);
    assert_eq!(g711.len(), 320);
    let back = g711_frame_to_l16(&g711);
    assert_eq!(back.len(), 1280);
    assert!(back.iter().all(|b| *b == 0), "silence in, silence out");
}

// TestBrowserLegJitterBuffer_ReordersAndFillsGaps (Go drives Session.jitter and forwardJitterOut
// directly; here the same two pieces without the session)
#[tokio::test(start_paused = true)]
async fn browser_leg_jitter_buffer_reorders_and_fills_gaps() {
    let path = UplinkPath::new(UplinkBuffer::new());
    let mut conv = UplinkConverter::new(path.gate());
    // Push seq 102 before seq 100 (reversed arrival order); seq 101 is never pushed (a lost
    // packet), so playout must still land as 100, silence, 102.
    let frame100 = vec![0x11u8; G711_SAMPLES_PER_FRAME];
    let frame102 = vec![0x33u8; G711_SAMPLES_PER_FRAME];
    path.on_rtp(G711_PAYLOAD_TYPE, 102, frame102.clone());
    path.on_rtp(G711_PAYLOAD_TYPE, 100, frame100.clone());
    let want: [Option<&Vec<u8>>; 3] = [Some(&frame100), None, Some(&frame102)];
    for (i, w) in want.iter().enumerate() {
        let payload = timeout(Duration::from_secs(2), path.jitter().recv())
            .await
            .unwrap_or_else(|_| panic!("frame {i}: timed out waiting on the output"))
            .unwrap();
        let got = conv.convert(&payload);
        match w {
            None => assert!(got.iter().all(|b| *b == 0), "frame {i}: want silence"),
            Some(g711) => assert_eq!(got, g711_frame_to_l16(g711), "frame {i}"),
        }
    }
    path.jitter().close();
}

// ---- codec_test.go -----------------------------------------------------------------------------

// TestAnswerToOpusFirstOfferNamesG722ThenPCMUOnly: the offer is the one a real pion peer wrote
// ("111 opus, 9 G722, 0 PCMU, 8 PCMA"), recorded in sfu_policy.json; the answer is the policy's.
// (The HandleOffer round trip through the real session is a slice 4b test.)
#[test]
fn answer_to_opus_first_offer_names_g722_then_pcmu_only() {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("tests/golden/sfu_policy.json");
    let doc: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(p).unwrap()).unwrap();
    let case = doc
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"].as_str().unwrap().starts_with("opus first"))
        .unwrap();
    let offer = case["offer"].as_str().unwrap();
    let m_audio = offer.lines().find(|l| l.starts_with("m=audio ")).unwrap();
    let offered: Vec<&str> = m_audio.split(' ').skip(3).collect();
    assert!(
        offered.len() >= 3 && offered[0] == "111",
        "the stand-in must offer Opus first: {offered:?}"
    );
    let answer = answer_codecs(offer).unwrap();
    let pts: Vec<u8> = answer.iter().map(|c| c.pt).collect();
    assert_eq!(pts, [9, 0], "G722 (9) then PCMU (0) and nothing else");
    assert!(answer
        .iter()
        .all(|c| !c.rtpmap_line().to_lowercase().contains("opus")));
    // audioTrack.Bind: the phone is sent the first of them
    assert_eq!(bind_codec(&answer), Some(PhoneCodec::G722));
}

// ---- wideband_test.go --------------------------------------------------------------------------

// TestG722KeepsTheUpperBandThatPCMUThrowsAway
#[test]
fn g722_keeps_the_upper_band_that_pcmu_throws_away() {
    let mut ph = 0.0;
    let mut enc = G722Encoder::default();
    let mut dec = G722Decoder::default();
    let (mut wide, mut narrow) = (Vec::new(), Vec::new());
    for _ in 0..25 {
        let fr = tone16k(640, 6000.0, 8000.0, &mut ph);
        let p = enc.encode(&fr);
        assert_eq!(
            p.len(),
            320,
            "a 40 ms frame is {} G.722 bytes, want 320",
            p.len()
        );
        wide.extend(dec.decode(&p));
        narrow.extend(decode_l16_frame(&g711_frame_to_l16(&l16_frame_to_g711(
            &encode_l16_frame(&fr),
        ))));
    }
    let (w, n) = (rms(&wide[4000..]), rms(&narrow[4000..]));
    let input = 8000.0 / 2f64.sqrt();
    assert!(
        w >= 0.7 * input,
        "G.722 6 kHz tone came out at rms {w:.0}, want about {input:.0}"
    );
    // The mu-law leg cannot carry 6 kHz: the box-filter downsampler folds it down (aliasing).
    let f = tone_freq(&narrow[4000..], 16000.0);
    assert!(
        (f - 6000.0).abs() >= 1000.0,
        "PCMU leg returned the 6 kHz tone as {f:.0} Hz (rms {n:.0}); the premise is gone"
    );
    let f = tone_freq(&wide[4000..], 16000.0);
    assert!(
        (f - 6000.0).abs() <= 120.0,
        "G.722 tone measured {f:.0} Hz, want 6000"
    );
}

// TestG722SilenceByteDecodesToNearSilence
#[test]
fn g722_silence_byte_decodes_to_near_silence() {
    let mut dec = G722Decoder::default();
    let frame = vec![g722_silence_byte(); 320];
    let mut out = Vec::new();
    for _ in 0..5 {
        out = dec.decode(&frame);
    }
    let p = peak(&out);
    assert!(
        p <= 300,
        "a lost-slot fill decodes to peak {p}, want near silence"
    );
}

// ---- the audio assertions of tone_test.go / wideband_test.go, without the WebRTC hop -----------

/// The phone-to-device direction: `ptime_ms` packets of `payload(i)` are pushed at the packet
/// rate for 3 s of (paused) time, `pt` on each, and what the `UplinkConverter` hands out is
/// returned as samples (Go: the loop of `testUplinkTone` against `Session.AudioOut`).
async fn uplink_run(pt: u8, ptime_ms: u64, mut payload: impl FnMut() -> Vec<u8>) -> Vec<i16> {
    let path = std::sync::Arc::new(UplinkPath::new(UplinkBuffer::new()));
    let (stop_tx, mut stop_rx) = tokio::sync::oneshot::channel::<()>();
    let reader = {
        let path = path.clone();
        tokio::spawn(async move {
            let mut conv = UplinkConverter::new(path.gate());
            let mut got = Vec::new();
            loop {
                tokio::select! {
                    f = path.jitter().recv() => match f {
                        Some(p) => got.extend(decode_l16_frame(&conv.convert(&p))),
                        None => break,
                    },
                    _ = &mut stop_rx => break,
                }
            }
            got
        })
    };
    let mut tk = interval(Duration::from_millis(ptime_ms));
    let start = tokio::time::Instant::now();
    let mut seq = 1000u16;
    while start.elapsed() < Duration::from_secs(3) {
        tk.tick().await;
        seq += 1;
        path.on_rtp(pt, seq, payload());
    }
    sleep(Duration::from_millis(300)).await;
    let _ = stop_tx.send(());
    let got = reader.await.unwrap();
    path.jitter().close();
    got
}

async fn uplink_tone_pcmu(ptime_ms: u64) {
    let hz = 880.0;
    let per = (8 * ptime_ms) as usize;
    let mut phase = 0.0f64;
    let got = uplink_run(G711_PAYLOAD_TYPE, ptime_ms, || {
        (0..per)
            .map(|_| {
                let b = encode_mu_law((8000.0 * phase.sin()) as i16);
                phase += 2.0 * PI * hz / 8000.0;
                b
            })
            .collect()
    })
    .await;
    // 3 s of audio is 48000 samples at 16 kHz; the buffer then plays silence for the tail.
    assert!(
        (40000..=54000).contains(&got.len()),
        "{ptime_ms} ms packets: {} samples",
        got.len()
    );
    let body = &got[8000..got.len() - 4000];
    let f = tone_freq(body, 16000.0);
    assert!(
        (f - hz).abs() <= hz * 0.02,
        "{ptime_ms} ms packets: tone measured {f:.1} Hz, want {hz}"
    );
    let p = peak(body);
    assert!(
        (6500..=9500).contains(&p),
        "{ptime_ms} ms packets: tone peak {p}"
    );
    let r = longest_run_below(body, 500);
    assert!(
        r <= 40,
        "{ptime_ms} ms packets: a {r}-sample silent stretch inside a continuous tone"
    );
}

// TestUplinkTone_Phone20msPackets (audio path only)
#[tokio::test(start_paused = true)]
async fn uplink_tone_phone_20ms_packets() {
    uplink_tone_pcmu(20).await;
}

// TestUplinkTone_Phone40msPackets (audio path only)
#[tokio::test(start_paused = true)]
async fn uplink_tone_phone_40ms_packets() {
    uplink_tone_pcmu(40).await;
}

// TestDownlinkTone_DeviceToPhone (audio path only): a 16 kHz tone in 1280-byte frames comes out
// of the PCMU encoder as the same tone at 8 kHz.
#[test]
fn downlink_tone_device_to_phone() {
    let hz = 440.0;
    let mut phase = 0.0;
    let mut enc = DownlinkEncoder::new();
    let mut pcm = Vec::new();
    let mut bytes = 0;
    for _ in 0..75 {
        let fr = tone16k(640, hz, 8000.0, &mut phase);
        let payload = enc
            .encode(PhoneCodec::Pcmu, &encode_l16_frame(&fr))
            .unwrap();
        bytes += payload.len();
        pcm.extend(decode_mu_law_frame(&payload));
    }
    assert_eq!(bytes, 75 * 320);
    let body = &pcm[4000..pcm.len() - 2000];
    let f = tone_freq(body, 8000.0);
    assert!(
        (f - hz).abs() <= hz * 0.02,
        "tone measured {f:.1} Hz at the phone"
    );
    let p = peak(body);
    assert!((7000..=9000).contains(&p), "tone peak {p}");
    assert!(longest_run_below(body, 500) <= 30);
}

// TestDownlinkWideband_DeviceToPhoneIsG722 (audio path only): 320 bytes per 40 ms frame, and the
// 6 kHz tone arrives.
#[test]
fn downlink_wideband_device_to_phone_is_g722() {
    let mut ph = 0.0;
    let mut enc = DownlinkEncoder::new();
    let mut dec = G722Decoder::default();
    let mut pcm = Vec::new();
    for _ in 0..50 {
        let fr = encode_l16_frame(&tone16k(640, 6000.0, 8000.0, &mut ph));
        let p = enc.encode(PhoneCodec::G722, &fr).unwrap();
        assert_eq!(p.len(), 320);
        pcm.extend(dec.decode(&p));
    }
    let body = &pcm[6000..pcm.len() - 2000];
    let f = tone_freq(body, 16000.0);
    assert!(
        (f - 6000.0).abs() <= 120.0,
        "tone measured {f:.0} Hz at the phone, want 6000"
    );
    assert!(rms(body) >= 0.7 * 8000.0 / 2f64.sqrt());
}

// TestUplinkWideband_PhoneToDeviceIsG722 (audio path only): 20 ms G.722 packets arrive as 16 kHz
// audio, not upsampled 8 kHz.
#[tokio::test(start_paused = true)]
async fn uplink_wideband_phone_to_device_is_g722() {
    let mut ph = 0.0;
    let mut enc = G722Encoder::default();
    let got = uplink_run(G722_PAYLOAD_TYPE, 20, || {
        let pl = enc.encode(&tone16k(320, 5000.0, 8000.0, &mut ph)); // 20 ms
        assert_eq!(
            pl.len(),
            160,
            "20 ms of G.722 is {} bytes, want 160",
            pl.len()
        );
        pl
    })
    .await;
    assert!(
        (40000..=54000).contains(&got.len()),
        "{} samples out of 3 s of audio",
        got.len()
    );
    let body = &got[8000..got.len() - 4000];
    let f = tone_freq(body, 16000.0);
    assert!(
        (f - 5000.0).abs() <= 100.0,
        "uplink tone measured {f:.0} Hz at the device, want 5000"
    );
    assert!(rms(body) >= 0.7 * 8000.0 / 2f64.sqrt());
}

// ---- tests for what the port adds ---------------------------------------------------------------

#[test]
fn downlink_encoder_drops_an_odd_g722_frame_instead_of_panicking() {
    let mut enc = DownlinkEncoder::new();
    assert!(enc.encode(PhoneCodec::G722, &[0u8; 1282]).is_none()); // 641 samples
    assert!(enc.encode(PhoneCodec::G722, &[0u8; 1280]).is_some());
    assert!(enc.encode(PhoneCodec::Pcmu, &[0u8; 1282]).is_some());
}

// readRemoteTrack's gate: only 0 and 9, only the first of them
#[tokio::test(start_paused = true)]
async fn uplink_path_admits_only_the_first_known_codec() {
    use transport_rs::sfu::audio::Admit;
    let path = UplinkPath::new(UplinkBuffer::new());
    assert_eq!(path.on_rtp(111, 1, vec![1; 160]), Admit::Foreign);
    assert_eq!(path.gate().codec(), None);
    assert_eq!(
        path.on_rtp(G722_PAYLOAD_TYPE, 2, vec![1; 160]),
        Admit::First
    );
    assert_eq!(
        path.on_rtp(G722_PAYLOAD_TYPE, 3, vec![1; 160]),
        Admit::Accepted
    );
    assert_eq!(
        path.on_rtp(G711_PAYLOAD_TYPE, 4, vec![1; 160]),
        Admit::Foreign
    );
    assert_eq!(path.foreign(), 2);
    assert_eq!(path.gate().codec(), Some(PhoneCodec::G722));
    path.jitter().close();
}

// A G.722 first packet sets the G.722 silence byte before the buffer exists: a lost slot is filled with it.
#[tokio::test(start_paused = true)]
async fn uplink_path_g722_lost_slot_uses_the_g722_silence_byte() {
    let path = UplinkPath::new(UplinkBuffer::new());
    path.on_rtp(G722_PAYLOAD_TYPE, 0, vec![1; 160]);
    path.on_rtp(G722_PAYLOAD_TYPE, 2, vec![3; 160]);
    let mut got = Vec::new();
    for _ in 0..3 {
        got.push(path.jitter().recv().await.unwrap());
    }
    assert_eq!(got[0], vec![1; 160]);
    assert_eq!(got[1], vec![g722_silence_byte(); 160]);
    assert_eq!(got[2], vec![3; 160]);
    path.jitter().close();
}

#[test]
fn uplink_timing_follows_the_first_packet() {
    use transport_rs::sfu::uplink::uplink_timing;
    let ms = Duration::from_millis;
    // the delay floor is 140 ms (2026-11-21: 60 ms lost 16 % of the slots on the tablet's Wi-Fi)
    assert_eq!(uplink_timing(160), (ms(20), ms(140)));
    assert_eq!(uplink_timing(320), (ms(40), ms(140)));
    assert_eq!(uplink_timing(80), (ms(10), ms(140)));
    assert_eq!(uplink_timing(1), (ms(10), ms(140)));
    assert_eq!(uplink_timing(360), (ms(45), ms(140)));
    assert_eq!(uplink_timing(960), (ms(120), ms(240)));
    assert_eq!(uplink_timing(5000), (ms(120), ms(240)));
}

#[test]
fn rtp_send_state_counts_like_go() {
    use transport_rs::sfu::RtpSendState;
    let mut s = RtpSendState::with_ssrc(7);
    assert_eq!((s.next_seq(), s.next_seq()), (0, 1));
    assert_eq!(
        (
            s.next_timestamp(320),
            s.next_timestamp(160),
            s.next_timestamp(1)
        ),
        (0, 320, 480)
    );
    for _ in 0..65534 {
        s.next_seq();
    }
    assert_eq!(s.next_seq(), 0, "wraps like uint16");
    assert_eq!(s.ssrc(), 7);
}

/// Encode and decode time of 1 s of audio, and that 8 s costs about 8 times as much (nothing
/// quadratic). A debug build is 10-30 times slower than release; the bounds are generous. Run
/// with `--nocapture` to see the numbers (README records them).
#[test]
fn speed_is_linear() {
    let mut ph = 0.0;
    let frame = tone16k(640, 1000.0, 8000.0, &mut ph);
    let secs = |n: usize| -> (f64, f64, f64) {
        let l16: Vec<u8> = (0..25 * n).flat_map(|_| encode_l16_frame(&frame)).collect();
        let t = Instant::now();
        let mut enc = G722Encoder::default();
        let mut payload = Vec::new();
        for f in l16.chunks(1280) {
            payload.extend(enc.encode(&decode_l16_frame(f)));
        }
        let t_enc = t.elapsed().as_secs_f64();
        let t = Instant::now();
        let mut dec = G722Decoder::default();
        let mut out = 0;
        for p in payload.chunks(320) {
            out += dec.decode(p).len();
        }
        let t_dec = t.elapsed().as_secs_f64();
        assert_eq!(out, 16000 * n);
        let t = Instant::now();
        let mut conv = 0;
        for f in l16.chunks(1280) {
            conv += g711_frame_to_l16(&l16_frame_to_g711(f)).len();
        }
        assert_eq!(conv, 32000 * n);
        (t_enc, t_dec, t.elapsed().as_secs_f64())
    };
    let (e1, d1, c1) = secs(1);
    let (e8, d8, c8) = secs(8);
    println!(
        "1 s of audio: G.722 encode {:.2} ms, decode {:.2} ms, PCMU round trip {:.2} ms; 8 s: {:.2} / {:.2} / {:.2} ms",
        e1 * 1e3, d1 * 1e3, c1 * 1e3, e8 * 1e3, d8 * 1e3, c8 * 1e3
    );
    assert!(e1 < 0.5 && d1 < 0.5 && c1 < 0.5, "1 s of audio is slow");
    assert!(e8 < 0.5 * 8.0 * 4.0 && d8 < 0.5 * 8.0 * 4.0 && c8 < 0.5 * 8.0 * 4.0);
}
