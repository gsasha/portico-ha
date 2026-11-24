//! Golden vectors produced by the REAL Go `internal/sfu` (tools/gen_golden, `GEN_ONLY=sfu`, see
//! README.md) replayed against the Rust port: G.711 and the converters, G.722 (the real
//! gotranspile/g722 library behind it), the uplink buffer scripts, and the answer-codec policy
//! of a real `sfu.Session`.

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::PathBuf;
use std::time::Duration;

use serde_json::Value;
use tokio::time::{sleep, timeout};
use transport_rs::sfu::g711::mu_law_segment;
use transport_rs::sfu::policy::answer_codecs;
use transport_rs::sfu::{
    decode_l16_frame, decode_mu_law, decode_mu_law_frame, downsample_16_to_8, encode_l16_frame,
    encode_mu_law, encode_mu_law_frame, g711_frame_to_l16, g722_silence_byte, l16_frame_to_g711,
    upsample_8_to_16, G722Decoder, G722Encoder, UplinkBuffer,
};

fn load(name: &str) -> Value {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("tests/golden");
    p.push(name);
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    serde_json::from_str(&text).unwrap()
}

fn unhex(s: &str) -> Vec<u8> {
    assert!(s.len().is_multiple_of(2), "odd hex");
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

fn hexs(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// "pcm hex": little-endian 16-bit samples.
fn pcm(s: &str) -> Vec<i16> {
    decode_l16_frame(&unhex(s))
}

fn pcm_hex(s: &[i16]) -> String {
    hexs(&encode_l16_frame(s))
}

fn st<'a>(v: &'a Value, k: &str) -> &'a str {
    v[k].as_str().unwrap_or("")
}

fn int(v: &Value, k: &str) -> i64 {
    v[k].as_i64().unwrap_or(0)
}

// ---- G.711 and the converters ----------------------------------------------------------------

#[test]
fn golden_mulaw_tables() {
    let doc = load("sfu_audio.json");
    let dec = doc["mulaw_decode"].as_array().unwrap();
    assert_eq!(dec.len(), 256);
    for (i, want) in dec.iter().enumerate() {
        assert_eq!(
            i64::from(decode_mu_law(i as u8)),
            want.as_i64().unwrap(),
            "decode {i:#04x}"
        );
    }
    let enc = unhex(st(&doc, "mulaw_encode_hex"));
    assert_eq!(enc.len(), 65536);
    for (i, want) in enc.iter().enumerate() {
        let sample = (i as i64 - 32768) as i16;
        assert_eq!(encode_mu_law(sample), *want, "encode {sample}");
    }
    let segs = doc["mulaw_segment"].as_array().unwrap();
    assert!(segs.len() > 40);
    for s in segs {
        let (v, want) = (s[0].as_i64().unwrap(), s[1].as_u64().unwrap());
        assert_eq!(mu_law_segment(v) as u64, want, "segment of {v}");
    }
}

/// Runs `f` over every {name, in, out} vector of `key` and counts them.
fn frames(doc: &Value, key: &str, f: impl Fn(&str, &str) -> String) -> usize {
    let list = doc[key].as_array().unwrap();
    assert!(!list.is_empty(), "{key}");
    for v in list {
        assert_eq!(
            f(st(v, "in"), st(v, "name")),
            st(v, "out"),
            "{key}: {}",
            st(v, "name")
        );
    }
    list.len()
}

#[test]
fn golden_g711_frames_and_resampler() {
    let doc = load("sfu_audio.json");
    let mut n = 0;
    n += frames(&doc, "mulaw_encode_frames", |i, _| {
        hexs(&encode_mu_law_frame(&pcm(i)))
    });
    n += frames(&doc, "mulaw_decode_frames", |i, _| {
        pcm_hex(&decode_mu_law_frame(&unhex(i)))
    });
    n += frames(&doc, "l16_to_g711", |i, _| {
        hexs(&l16_frame_to_g711(&unhex(i)))
    });
    n += frames(&doc, "g711_to_l16", |i, _| {
        hexs(&g711_frame_to_l16(&unhex(i)))
    });
    n += frames(&doc, "decode_l16", |i, _| {
        pcm_hex(&decode_l16_frame(&unhex(i)))
    });
    n += frames(&doc, "encode_l16", |i, _| hexs(&encode_l16_frame(&pcm(i))));
    n += frames(&doc, "downsample16to8", |i, _| {
        pcm_hex(&downsample_16_to_8(&pcm(i)))
    });
    n += frames(&doc, "upsample8to16", |i, _| {
        pcm_hex(&upsample_8_to_16(&pcm(i)))
    });
    assert_eq!(n, 272, "only {n} vectors"); // exact (R2-107): a truncated golden file must fail
}

// ---- G.722 -----------------------------------------------------------------------------------

#[test]
fn golden_g722_codec_scenarios_are_bit_exact() {
    let doc = load("sfu_g722.json");
    assert_eq!(int(&doc, "silence_byte"), i64::from(g722_silence_byte()));
    let scs = doc["codec_scenarios"].as_array().unwrap();
    assert_eq!(scs.len(), 27);
    let mut frames = 0;
    for sc in scs {
        let mut enc = G722Encoder::default();
        let mut dec = G722Decoder::default();
        for (n, f) in sc["frames"].as_array().unwrap().iter().enumerate() {
            let got_enc = enc.encode(&pcm(st(f, "pcm")));
            assert_eq!(
                hexs(&got_enc),
                st(f, "enc"),
                "{} frame {n}: encode",
                st(sc, "name")
            );
            let got_dec = dec.decode(&got_enc);
            assert_eq!(
                pcm_hex(&got_dec),
                st(f, "dec"),
                "{} frame {n}: decode",
                st(sc, "name")
            );
            frames += 1;
        }
    }
    assert_eq!(frames, 292, "{frames} frames"); // exact (R2-107): a truncated golden file must fail
}

#[test]
fn golden_g722_decoder_alone() {
    let doc = load("sfu_g722.json");
    let scs = doc["decode_scenarios"].as_array().unwrap();
    assert_eq!(scs.len(), 263);
    for sc in scs {
        let mut dec = G722Decoder::default();
        for (n, f) in sc["frames"].as_array().unwrap().iter().enumerate() {
            let got = dec.decode(&unhex(st(f, "in")));
            assert_eq!(pcm_hex(&got), st(f, "out"), "{} frame {n}", st(sc, "name"));
        }
    }
}

#[test]
fn golden_g722_odd_lengths_panic_where_go_does() {
    let doc = load("sfu_g722.json");
    let odds = doc["odd_lengths"].as_array().unwrap();
    assert_eq!(odds.len(), 10); // exact (R2-107): a truncated golden file must fail
    let mut panicked = 0;
    for o in odds {
        let samples = pcm(st(o, "pcm"));
        let r = catch_unwind(AssertUnwindSafe(|| {
            G722Encoder::default().encode(&samples).len()
        }));
        match r {
            Ok(len) => {
                assert!(
                    !o["panics"].as_bool().unwrap(),
                    "{}: Go panics",
                    st(o, "name")
                );
                assert_eq!(len as i64, int(o, "out_len"), "{}", st(o, "name"));
            }
            Err(_) => {
                assert!(
                    o["panics"].as_bool().unwrap(),
                    "{}: Go does not panic",
                    st(o, "name")
                );
                panicked += 1;
            }
        }
    }
    assert_eq!(panicked, 6); // exact (R2-107): a truncated golden file must fail
}

// ---- uplink ----------------------------------------------------------------------------------

fn rle(b: &[u8]) -> String {
    if !b.is_empty() && b.iter().all(|x| *x == b[0]) {
        return format!("{:02x}*{}", b[0], b.len());
    }
    hexs(b)
}

fn payload_of(op: &Value) -> Vec<u8> {
    let (fill, len) = (int(op, "fill"), int(op, "len") as usize);
    let ramp = op["ramp"].as_bool().unwrap_or(false);
    (0..len)
        .map(|i| {
            if ramp {
                (fill + i as i64) as u8
            } else {
                fill as u8
            }
        })
        .collect()
}

async fn run_uplink(sc: &Value) -> Vec<Value> {
    // the Go scripts were recorded against Go's 60 ms delay floor
    let u = UplinkBuffer::with_min_delay(transport_rs::sfu::uplink::GO_MIN_UPLINK_DELAY);
    let mut res = Vec::new();
    for op in sc["ops"].as_array().unwrap() {
        let mut r = serde_json::Map::new();
        match st(op, "op") {
            "push" => u.push(int(op, "seq") as u16, payload_of(op)),
            "silence" => u.set_silence(int(op, "silence") as u8),
            "recv" => {
                let t = match int(op, "timeout_ms") {
                    0 => 700,
                    t => t as u64,
                };
                // A closed buffer ends recv at once (Go would block): report it as a timeout.
                let out = match timeout(Duration::from_millis(t), u.recv()).await {
                    Ok(Some(f)) => rle(&f),
                    _ => "timeout".to_string(),
                };
                r.insert("out".into(), Value::from(out));
            }
            "sleep" => sleep(Duration::from_millis(int(op, "ms") as u64)).await,
            "drain" => {
                let mut outs = Vec::new();
                while let Some(f) = u.try_recv() {
                    outs.push(rle(&f));
                }
                if !outs.is_empty() {
                    r.insert("outs".into(), Value::from(outs));
                }
            }
            "close" => u.close(),
            other => panic!("unknown op {other}"),
        }
        res.push(Value::Object(r));
    }
    res
}

#[tokio::test(start_paused = true)]
async fn golden_uplink_scripts() {
    let doc = load("sfu_uplink.json");
    let scs = doc.as_array().unwrap();
    assert_eq!(scs.len(), 33);
    for sc in scs {
        let got = run_uplink(sc).await;
        let want = sc["results"].as_array().unwrap();
        assert_eq!(got.len(), want.len());
        for (idx, (g, w)) in got.iter().zip(want).enumerate() {
            let w = if w.is_null() {
                serde_json::json!({})
            } else {
                w.clone()
            };
            assert_eq!(
                *g,
                w,
                "uplink scenario {:?}, op #{idx} {}",
                st(sc, "name"),
                sc["ops"][idx]
            );
        }
    }
}

// ---- answer-codec policy ---------------------------------------------------------------------

#[test]
fn golden_answer_codec_policy() {
    let doc = load("sfu_policy.json");
    let cases = doc.as_array().unwrap();
    assert_eq!(cases.len(), 43); // exact (R2-107): a truncated golden file must fail
    let (mut errs, mut oks) = (0, 0);
    for c in cases {
        let name = st(c, "name");
        match answer_codecs(st(c, "offer")) {
            Err(e) => {
                assert_eq!(e.to_string(), st(c, "err"), "{name}");
                errs += 1;
            }
            Ok(codecs) => {
                assert_eq!(st(c, "err"), "", "{name}: Go refused the offer");
                let pts: Vec<String> = codecs.iter().map(|c| c.pt.to_string()).collect();
                let want_pts: Vec<&str> = c["pts"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|v| v.as_str().unwrap())
                    .collect();
                assert_eq!(pts, want_pts, "{name}: payload types");
                let rtpmap: Vec<String> = codecs.iter().map(|c| c.rtpmap_line()).collect();
                let want: Vec<&str> = c["rtpmap"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|v| v.as_str().unwrap())
                    .collect();
                assert_eq!(rtpmap, want, "{name}: rtpmap");
                let fmtp: Vec<String> = codecs.iter().filter_map(|c| c.fmtp_line()).collect();
                let want: Vec<&str> = c["fmtp"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|v| v.as_str().unwrap())
                    .collect();
                assert_eq!(fmtp, want, "{name}: fmtp");
                let m = format!("m=audio PORT UDP/TLS/RTP/SAVPF {}", pts.join(" "));
                assert_eq!(m, st(c, "m_line"), "{name}: m-line");
                oks += 1;
            }
        }
    }
    assert_eq!((oks, errs), (34, 9), "answered, refused"); // exact (R2-107)
}

// ---- real arrival patterns through the uplink buffer ---------------------------------------------

/// Three real Firefox 153 G.722 uplinks (arrival time and sequence number of every packet, recorded
/// with `PORTICO_SFU_TRACE_RTP=1` through the Rust binary, `tests/golden/sfu_uplink_arrivals.json`)
/// replayed through the uplink buffer on a paused clock. The ASSERTED counts (18, 14, 13) are
/// Rust's: this is not a Go-derived vector. The same arrival times were replayed once through the
/// real Go `pkg/sip` JitterBuffer in real time by a throwaway program (3 runs each: 18, 13/24/18
/// and 14/9/11 slots of about 760); those numbers are quoted for comparison, not committed as a
/// vector. The numbers are the point: a fixed-delay
/// buffer whose window is 9 frames loses a slot about every second to a sender whose clock runs
/// 1.3 % fast, whatever the port, and loses far more when the tick phase is unlucky (PARITY.md,
/// "the uplink buffer's phase sensitivity").
#[tokio::test(start_paused = true)]
async fn real_firefox_arrival_traces_lose_what_the_go_buffer_loses() {
    let v = load("sfu_uplink_arrivals.json");
    let want = [
        ("firefox_h3_run1", 18u32),
        ("firefox_h3_run2", 14),
        ("firefox_h3_run3", 13),
    ];
    for (name, expect) in want {
        let rows: Vec<(u64, u16)> = v["traces"][name]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| (r[0].as_u64().unwrap(), r[1].as_u64().unwrap() as u16))
            .collect();
        let ub = UplinkBuffer::with_min_delay(transport_rs::sfu::uplink::GO_MIN_UPLINK_DELAY);
        ub.set_silence(0xFB);
        let reader = {
            let ub = ub.clone();
            tokio::spawn(async move {
                let (mut real, mut silent) = (0u32, 0u32);
                while let Some(p) = ub.recv().await {
                    if p[0] == 0x11 {
                        real += 1;
                    } else {
                        silent += 1;
                    }
                }
                (real, silent)
            })
        };
        let t0 = tokio::time::Instant::now();
        for (t, seq) in &rows {
            tokio::time::sleep_until(t0 + Duration::from_millis(*t - rows[0].0)).await;
            ub.push(*seq, vec![0x11; 160]);
        }
        sleep(Duration::from_millis(300)).await;
        ub.close();
        let (real, silent) = reader.await.unwrap();
        assert_eq!(
            silent, expect,
            "{name}: {real} slots played, {silent} silent"
        );
        assert!(real > 700);
    }
}

/// The same real Firefox traces through the production buffer (140 ms floor, 2026-11-21): the
/// slots the Go buffer's 60 ms window lost are played. Quoted loss with Go's delay is in the test
/// above (18, 14, 13 of about 760); a 1.3 % fast sender needs a window of several frames.
#[tokio::test(start_paused = true)]
async fn real_firefox_arrival_traces_lose_far_less_with_the_140_ms_delay() {
    let v = load("sfu_uplink_arrivals.json");
    for (name, go_lost) in [("firefox_h3_run1", 18u32), ("firefox_h3_run2", 14), ("firefox_h3_run3", 13)] {
        let rows: Vec<(u64, u16)> = v["traces"][name]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| (r[0].as_u64().unwrap(), r[1].as_u64().unwrap() as u16))
            .collect();
        let ub = UplinkBuffer::new();
        ub.set_silence(0xFB);
        let reader = {
            let ub = ub.clone();
            tokio::spawn(async move {
                let mut silent = 0u32;
                while let Some(p) = ub.recv().await {
                    silent += u32::from(p[0] != 0x11);
                }
                silent
            })
        };
        let t0 = tokio::time::Instant::now();
        for (t, seq) in &rows {
            tokio::time::sleep_until(t0 + Duration::from_millis(*t - rows[0].0)).await;
            ub.push(*seq, vec![0x11; 160]);
        }
        sleep(Duration::from_millis(300)).await;
        ub.close();
        let silent = reader.await.unwrap();
        assert!(silent * 4 <= go_lost, "{name}: {silent} silent slots with 140 ms, {go_lost} with Go's 60 ms");
    }
}
