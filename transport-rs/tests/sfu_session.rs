//! The ported Go tests of `internal/sfu` that drive a real WebRTC pair (`sfu_test.go`,
//! `codec_test.go`, `tone_test.go`, `wideband_test.go`), against the `str0m` session. The Go tests
//! use a pion peer as the browser; here the peer is a second `str0m` instance as the OFFERER
//! (`tests/common/peer.rs`) over loopback. The Go name is in a comment on each test.

mod common;

use std::f64::consts::PI;
use std::sync::Arc;
use std::time::{Duration, Instant};

use common::peer::{Peer, PeerOpts};
use tokio::time::sleep;
use transport_rs::sfu::sdp::audio_payload_types;
use transport_rs::sfu::session::{Session, SessionConfig};
use transport_rs::sfu::{
    decode_l16_frame, decode_mu_law_frame, encode_l16_frame, encode_mu_law_frame,
    g711_frame_to_l16, G722Decoder, PhoneCodec,
};

// ---- helpers of tone_test.go / wideband_test.go -----------------------------------------------

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

fn tone16k(n: usize, hz: f64, amp: f64, phase: &mut f64) -> Vec<i16> {
    (0..n)
        .map(|_| {
            let v = (amp * phase.sin()) as i16;
            *phase += 2.0 * PI * hz / 16000.0;
            v
        })
        .collect()
}

/// `hostOnlySettingEngine`: loopback only.
fn cfg() -> SessionConfig {
    SessionConfig::loopback()
}

fn pcmu_peer() -> PeerOpts {
    PeerOpts::default()
}

fn g722_peer() -> PeerOpts {
    PeerOpts {
        pcmu: false,
        g722: true,
        ..PeerOpts::default()
    }
}

/// `connectedPairWith`: a real offer/answer between a browser stand-in and a Session.
async fn connected_pair(o: PeerOpts) -> (Arc<Session>, Peer) {
    let peer = Peer::new(o).await;
    let session = Session::with_config("tone-call", cfg()).unwrap();
    let offer = peer.offer().await;
    let answer = session.handle_offer(&offer).await.expect("handle_offer");
    peer.accept_answer(&answer).await.expect("accept_answer");
    assert!(peer.wait_connected(10).await, "never connected");
    let end = Instant::now() + Duration::from_secs(5);
    while !session.connected() && Instant::now() < end {
        sleep(Duration::from_millis(20)).await;
    }
    assert!(
        session.connected(),
        "the session never reached Connected from its own side"
    );
    (session, peer)
}

// TestSession_RealICEDTLSHandshakeAndAudioBridging
#[tokio::test]
async fn real_ice_dtls_handshake_and_audio_bridging() {
    let (session, peer) = connected_pair(pcmu_peer()).await;

    // Device -> browser: a raw L16 frame arrives as a real G.711 RTP packet decoding back to
    // (approximately) the same audio.
    let device_frame = encode_l16_frame(&vec![6000i16; 640]);
    session.audio_in().send(device_frame).await.unwrap();
    let end = Instant::now() + Duration::from_secs(5);
    let got = loop {
        if let Some(p) = peer.received().first().cloned() {
            break p;
        }
        assert!(
            Instant::now() < end,
            "the browser never received the device's audio over real RTP"
        );
        sleep(Duration::from_millis(20)).await;
    };
    assert_eq!(got.payload.len(), 320, "G.711 payload size");
    assert_eq!(got.pt, 0);
    for (i, s) in decode_mu_law_frame(&got.payload).iter().enumerate() {
        assert!((i32::from(*s) - 6000).abs() <= 400, "sample {i} = {s}");
    }

    // Browser -> device: the browser's mic sends a G.711 packet; AudioOut delivers L16.
    let mic = encode_mu_law_frame(&vec![-7000i16; 320]);
    peer.send(false, 0, mic);
    let out = session.audio_out();
    let frame = tokio::time::timeout(Duration::from_secs(5), async {
        out.lock().await.recv().await
    })
    .await
    .expect("session.AudioOut never received the browser's mic audio over real RTP")
    .unwrap();
    assert_eq!(frame.len(), 1280);
    for (i, s) in decode_l16_frame(&frame).iter().enumerate() {
        assert!((i32::from(*s) + 7000).abs() <= 400, "sample {i} = {s}");
    }
    session.close();
    session.wait_done().await;
}

// TestSession_ICECandidateFromBrowser
#[tokio::test]
async fn ice_candidate_from_browser() {
    let peer = Peer::new(pcmu_peer()).await;
    let session = Session::with_config("test-call-2", cfg()).unwrap();
    let answer = session.handle_offer(&peer.offer().await).await.unwrap();
    assert!(
        !answer.is_empty(),
        "handle_offer returned an empty answer SDP"
    );
    // a well-formed but unreachable candidate is accepted without error (format, not reachability)
    session
        .add_ice_candidate("candidate:1 1 UDP 2130706431 127.0.0.1 12345 typ host")
        .await
        .unwrap();
    // the end-of-candidates marker in both spellings, and an mDNS candidate (ignored)
    session.add_ice_candidate("").await.unwrap();
    session
        .add_ice_candidate("a=end-of-candidates")
        .await
        .unwrap();
    session
        .add_ice_candidate("candidate:2 1 udp 2130706431 abcd-1234.local 5000 typ host")
        .await
        .unwrap();
    let e = session.add_ice_candidate("garbage").await.unwrap_err();
    assert!(e.starts_with("sfu: adding ICE candidate: "), "{e}");
    session.close();
}

// the Go text of a candidate that arrives before any offer was accepted (a session left in the
// registry by a failed offer): measured against the real Go binary
#[tokio::test]
async fn a_candidate_or_answer_before_an_accepted_offer_is_refused_with_gos_text() {
    let session = Session::with_config("early", cfg()).unwrap();
    assert_eq!(
        session
            .add_ice_candidate("candidate:1 1 udp 2130706431 127.0.0.1 5000 typ host")
            .await
            .unwrap_err(),
        "sfu: adding ICE candidate: InvalidStateError: remote description is not set"
    );
    assert_eq!(
        session.add_ice_candidate("").await.unwrap_err(),
        "sfu: adding ICE candidate: InvalidStateError: remote description is not set"
    );
    assert_eq!(
        session.handle_answer("v=0\r\n").await.unwrap_err(),
        "sfu: setting remote answer: InvalidModificationError: invalid proposed signaling state transition: stable->SetRemote(answer)->stable"
    );
    assert_eq!(
        session.handle_answer("garbage").await.unwrap_err(),
        "sfu: setting remote answer: failed to unmarshal SDP: sdp: syntax error at pos 1: \"a\""
    );
    session.close();
}

// PARITY 325 (R1-77, R2-38): only the syntax error of the FIRST line of an answer is Go's text. An
// answer whose first line is fine and a later line is not gets the stable-state text (Go
// unmarshals the whole SDP first). A recorded limit, pinned so that a change of it is seen.
#[tokio::test]
async fn a_later_line_error_in_an_answer_gets_the_stable_state_text() {
    let session = Session::with_config("late", cfg()).unwrap();
    assert_eq!(
        session.handle_answer("v=0\r\nzz\r\n").await.unwrap_err(),
        "sfu: setting remote answer: InvalidModificationError: invalid proposed signaling state transition: stable->SetRemote(answer)->stable"
    );
    session.close();
}

// TestEncodeLoopStopsOnClose
#[tokio::test]
async fn encode_loop_stops_on_close() {
    let session = Session::with_config("test-call-3", cfg()).unwrap();
    session.close();
    // Closing twice must not panic (main.go's error paths may call it more than once).
    session.close();
    tokio::time::timeout(Duration::from_secs(2), session.wait_done())
        .await
        .expect("the session task did not end after Close");
    // the encode loop is gone: nobody takes frames any more, and a send neither blocks nor panics
    for _ in 0..20 {
        let _ = session.audio_in().try_send(vec![0; 1280]);
    }
}

// TestSession_OnDisconnectedFiresOnceOnClose
#[tokio::test]
async fn on_disconnected_fires_once_on_close() {
    let s = Session::with_config("gone-1", cfg()).unwrap();
    let (tx, rx) = std::sync::mpsc::channel::<()>();
    let t1 = tx.clone();
    s.on_disconnected(move || t1.send(()).unwrap());
    sleep(Duration::from_millis(100)).await;
    assert!(
        rx.try_recv().is_err(),
        "OnDisconnected fired before the session was gone"
    );
    s.close();
    assert!(
        rx.recv_timeout(Duration::from_secs(2)).is_ok(),
        "OnDisconnected did not fire on Close"
    );
    s.close();
    let (late_tx, late_rx) = std::sync::mpsc::channel::<()>();
    s.on_disconnected(move || late_tx.send(()).unwrap());
    assert!(
        late_rx.recv_timeout(Duration::from_secs(1)).is_ok(),
        "a callback registered after the session was gone did not run at once"
    );
    sleep(Duration::from_millis(100)).await;
    assert!(rx.try_recv().is_err(), "the first callback ran twice");
}

// TestBrowserLegJitterBuffer_ReordersAndFillsGaps: through Session.jitter() and AudioOut
#[tokio::test]
async fn browser_leg_jitter_buffer_reorders_and_fills_gaps() {
    let session = Session::with_config("test-call-jitter", cfg()).unwrap();
    let distinct = |b: u8| vec![b; 320];
    let (f100, f102) = (distinct(0x11), distinct(0x33));
    // seq 102 before 100 (reversed), 101 never pushed: playout is 100, silence, 102
    session.jitter().push(102, f102.clone());
    session.jitter().push(100, f100.clone());
    let out = session.audio_out();
    for (i, want) in [Some(f100), None, Some(f102)].into_iter().enumerate() {
        let got = tokio::time::timeout(Duration::from_secs(2), async {
            out.lock().await.recv().await
        })
        .await
        .unwrap_or_else(|_| panic!("frame {i}: timed out waiting on AudioOut"))
        .unwrap();
        match want {
            None => assert!(got.iter().all(|b| *b == 0), "frame {i}: want silence"),
            Some(g711) => assert_eq!(got, g711_frame_to_l16(&g711), "frame {i}"),
        }
    }
    session.close();
}

fn pts_of_answer(a: &str) -> Vec<u8> {
    audio_payload_types(a)
}

// TestAnswerToOpusFirstOfferNamesG722ThenPCMUOnly (HandleOffer through str0m)
#[tokio::test]
async fn answer_to_opus_first_offer_names_g722_then_pcmu_only() {
    // a phone-like offerer: "111 opus, 9 G722, 0 PCMU"
    let peer = Peer::new(PeerOpts {
        opus: true,
        g722: true,
        pcmu: true,
        ..PeerOpts::default()
    })
    .await;
    let offer = peer.offer().await;
    assert_eq!(
        pts_of_answer(&offer),
        vec![111, 9, 0],
        "the stand-in must offer Opus first"
    );
    let s = Session::with_config("codec-call", cfg()).unwrap();
    let answer = s.handle_offer(&offer).await.unwrap();
    assert_eq!(
        pts_of_answer(&answer),
        vec![9, 0],
        "G722 (9) then PCMU (0) and nothing else:\n{answer}"
    );
    assert!(
        !answer.to_lowercase().contains("opus"),
        "the answer still carries Opus"
    );
    let n = s.negotiated();
    assert_eq!(
        (n.g722_pt, n.pcmu_pt, n.send),
        (Some(9), Some(0), Some(PhoneCodec::G722))
    );
    s.close();
}

fn pcmu_first(text: String) -> String {
    text.replace("SAVPF 111 9 0", "SAVPF 0 9 111")
}

// the offerer's order decides (Go answers in the offerer's order): PCMU listed first is sent PCMU
#[tokio::test]
async fn an_offerer_that_lists_pcmu_first_is_answered_pcmu_first_and_sent_pcmu() {
    let o = PeerOpts {
        opus: true,
        g722: true,
        pcmu: true,
        rewrite: Some(pcmu_first),
        ..PeerOpts::default()
    };
    let peer = Peer::new(o).await;
    let offer = peer.offer().await;
    assert_eq!(pts_of_answer(&offer), vec![0, 9, 111]);
    let s = Session::with_config("pcmu-first", cfg()).unwrap();
    let answer = s.handle_offer(&offer).await.unwrap();
    assert_eq!(pts_of_answer(&answer), vec![0, 9]);
    assert_eq!(s.negotiated().send, Some(PhoneCodec::Pcmu));
    s.close();
}

/// R2-20: what is on the WIRE. A 440 Hz tone from the device, 1.2 s, through a real pair whose
/// offer lists the codecs in the order of `o`: every packet carries `want_pt`, 320 bytes, and
/// decodes (with the decoder of that payload type) to the tone. A downlink that encoded G.722 for
/// a PCMU-first phone would send noise under payload type 0.
async fn downlink_on_the_wire(o: PeerOpts, want_pt: u8) {
    let (session, peer) = connected_pair(o).await;
    let mut ph = 0.0f64;
    let mut tk = tokio::time::interval(Duration::from_millis(40));
    for _ in 0..30 {
        tk.tick().await;
        session
            .audio_in()
            .send(encode_l16_frame(&tone16k(640, 440.0, 8000.0, &mut ph)))
            .await
            .unwrap();
    }
    sleep(Duration::from_millis(200)).await;
    let rx = peer.received();
    assert!(
        rx.len() >= 20,
        "only {} packets reached the phone",
        rx.len()
    );
    let mut pcm: Vec<i16> = Vec::new();
    let mut dec = G722Decoder::new();
    for p in &rx {
        assert_eq!(p.pt, want_pt, "payload type on the wire");
        assert_eq!(p.payload.len(), 320, "payload size on the wire");
        if want_pt == 0 {
            pcm.extend(decode_mu_law_frame(&p.payload));
        } else {
            pcm.extend(dec.decode(&p.payload));
        }
    }
    let rate = if want_pt == 0 { 8000.0 } else { 16000.0 };
    let body = &pcm[pcm.len() / 4..];
    let f = tone_freq(body, rate);
    assert!(
        (f - 440.0).abs() <= 10.0,
        "payload type {want_pt}: the tone decodes to {f:.1} Hz, want 440"
    );
    let pk = peak(body);
    assert!(
        (6000..=9000).contains(&pk),
        "payload type {want_pt}: peak {pk}, want about 8000"
    );
    session.close();
}

// R2-20: PCMU listed first (G.722 also offered) is SENT PCMU: pt 0, mu-law of the tone
#[tokio::test]
async fn a_pcmu_first_phone_gets_mu_law_on_the_wire() {
    downlink_on_the_wire(
        PeerOpts {
            opus: true,
            g722: true,
            pcmu: true,
            rewrite: Some(pcmu_first),
            ..PeerOpts::default()
        },
        0,
    )
    .await;
}

// R2-20, the mirror: G.722 listed first (PCMU also offered) is SENT G.722: pt 9
#[tokio::test]
async fn a_g722_first_phone_gets_g722_on_the_wire() {
    downlink_on_the_wire(
        PeerOpts {
            opus: true,
            g722: true,
            pcmu: true,
            ..PeerOpts::default()
        },
        9,
    )
    .await;
}

/// The 43 recorded offers of `sfu_policy.json` (the real Go answers) through `HandleOffer` on
/// str0m: every answered offer names the same payload types in the same order, but the listed
/// divergences; every refused offer is refused.
#[tokio::test]
async fn the_recorded_offers_get_gos_answers_through_str0m() {
    let text = std::fs::read_to_string(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/golden/sfu_policy.json"
    ))
    .unwrap();
    let v: serde_json::Value = serde_json::from_str(&text).unwrap();
    let mut differ: Vec<String> = Vec::new();
    let n = v.as_array().unwrap().len();
    assert_eq!(n, 43);
    for c in v.as_array().unwrap() {
        let name = c["name"].as_str().unwrap();
        let go: Vec<u8> = c["pts"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_str().unwrap().parse().unwrap())
            .collect();
        let s = Session::with_config("g", cfg()).unwrap();
        let r = s.handle_offer(c["offer"].as_str().unwrap()).await;
        s.close();
        if let Ok(a) = &r {
            // the answer is clean: no attribute line of a payload type its m= line does not name
            let pts = audio_payload_types(a);
            for l in a.lines() {
                for k in ["a=rtpmap:", "a=fmtp:", "a=rtcp-fb:"] {
                    if let Some(rest) = l.strip_prefix(k) {
                        if let Ok(pt) = rest.split(' ').next().unwrap_or("").parse::<u8>() {
                            assert!(
                                pts.contains(&pt),
                                "{name}: {l} but the m= line names {pts:?}"
                            );
                        }
                    }
                }
            }
        }
        let ok = match &r {
            Ok(a) => audio_payload_types(a) == go && !go.is_empty(),
            Err(_) => go.is_empty(),
        };
        if !ok {
            differ.push(name.to_string());
        }
    }
    differ.sort();
    // Known divergences, all in offers no browser sends (PARITY.md, slice 4b): str0m refuses an
    // offer whose m-line names a type with no usable rtpmap or a dynamic type without a clock
    // rate where pion answered; and it accepts a dynamic G.722 at clock 16000 where pion refused.
    let mut want = vec![
        "dynamic g722 (96) without a clock rate",
        "dynamic g722 at clock 16000",
        "dynamic pcmu (97) without a clock rate",
        "m-line names a payload type with no rtpmap",
    ];
    want.sort();
    assert_eq!(differ, want, "the divergence list changed");
}

// ---- tone_test.go / wideband_test.go over a real offer/answer pair ---------------------------

/// phone -> device: the phone's mic tone, as `ptime_ms` packets, comes out of AudioOut as the
/// same tone at the same speed, with no gaps, at 16 kHz.
async fn uplink_tone(ptime_ms: u64) {
    let (session, peer) = connected_pair(pcmu_peer()).await;
    let hz = 880.0;
    peer.tone(false, hz, ptime_ms, 8000.0, 3.0);
    let out = session.audio_out();
    let mut got: Vec<i16> = Vec::new();
    let start = Instant::now();
    while start.elapsed() < Duration::from_millis(3300) {
        if let Ok(Some(f)) = tokio::time::timeout(Duration::from_millis(100), async {
            out.lock().await.recv().await
        })
        .await
        {
            got.extend(decode_l16_frame(&f));
        }
    }
    // the peer's tone starts a little after the pair connected; the jitter buffer pads the tail
    assert!(
        (40000..=54000).contains(&got.len()),
        "{ptime_ms} ms packets: {} samples came out, want about 48000",
        got.len()
    );
    let body: Vec<i16> = got[8000..got.len() - 4000].to_vec();
    let f = tone_freq(&body, 16000.0);
    assert!(
        (f - hz).abs() <= hz * 0.02,
        "{ptime_ms} ms packets: tone measured {f:.1} Hz, want {hz}"
    );
    let p = peak(&body);
    assert!(
        (6500..=9500).contains(&p),
        "{ptime_ms} ms packets: tone peak {p}, want about 8000"
    );
    let r = longest_run_below(&body, 500);
    assert!(
        r <= 40,
        "{ptime_ms} ms packets: a {r}-sample silent stretch inside a continuous tone"
    );
    session.close();
}

// TestUplinkTone_Phone20msPackets
#[tokio::test]
async fn uplink_tone_phone_20ms_packets() {
    uplink_tone(20).await;
}

// TestUplinkTone_Phone40msPackets
#[tokio::test]
async fn uplink_tone_phone_40ms_packets() {
    uplink_tone(40).await;
}

// TestDownlinkTone_DeviceToPhone
#[tokio::test]
async fn downlink_tone_device_to_phone() {
    let (session, peer) = connected_pair(pcmu_peer()).await;
    let hz = 440.0;
    let mut phase = 0.0f64;
    let mut tk = tokio::time::interval(Duration::from_millis(40));
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(3) {
        tk.tick().await;
        let fr: Vec<i16> = (0..640)
            .map(|_| {
                let v = (8000.0 * phase.sin()) as i16;
                phase += 2.0 * PI * hz / 16000.0;
                v
            })
            .collect();
        session
            .audio_in()
            .send(encode_l16_frame(&fr))
            .await
            .unwrap();
    }
    sleep(Duration::from_millis(300)).await;
    let rx = peer.received();
    let bytes: usize = rx.iter().map(|p| p.payload.len()).sum();
    assert!(
        (20000..=25000).contains(&bytes),
        "{} packets, {bytes} PCMU bytes reached the phone, want about 24000",
        rx.len()
    );
    let pcm: Vec<i16> = rx
        .iter()
        .flat_map(|p| decode_mu_law_frame(&p.payload))
        .collect();
    let body = &pcm[4000..pcm.len() - 2000];
    let f = tone_freq(body, 8000.0);
    assert!(
        (f - hz).abs() <= hz * 0.02,
        "tone measured {f:.1} Hz at the phone, want {hz}"
    );
    let p = peak(body);
    assert!(
        (7000..=9000).contains(&p),
        "tone peak {p} at the phone, want about 8000"
    );
    assert!(
        longest_run_below(body, 500) <= 30,
        "a silent stretch inside a continuous tone"
    );
    // 40 ms of audio is one packet of 320 bytes and the RTP clock advances by the payload bytes
    assert!(rx.iter().all(|p| p.pt == 0 && p.payload.len() == 320));
    for w in rx.windows(2) {
        assert_eq!(w[1].ts.wrapping_sub(w[0].ts), 320, "timestamp step");
        assert_eq!(w[1].seq.wrapping_sub(w[0].seq), 1, "sequence step");
    }
    session.close();
}

// TestDownlinkWideband_DeviceToPhoneIsG722
#[tokio::test]
async fn downlink_wideband_device_to_phone_is_g722() {
    let (session, peer) = connected_pair(g722_peer()).await;
    assert_eq!(session.negotiated().send, Some(PhoneCodec::G722));
    let mut ph = 0.0f64;
    let mut tk = tokio::time::interval(Duration::from_millis(40));
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(2) {
        tk.tick().await;
        session
            .audio_in()
            .send(encode_l16_frame(&tone16k(640, 6000.0, 8000.0, &mut ph)))
            .await
            .unwrap();
    }
    sleep(Duration::from_millis(200)).await;
    let rx = peer.received();
    assert!(
        rx.len() >= 40,
        "only {} packets in 2 s, want about 50",
        rx.len()
    );
    let mut dec = G722Decoder::new();
    let mut pcm: Vec<i16> = Vec::new();
    for p in &rx {
        assert_eq!(p.pt, 9, "the phone offered G.722 only");
        assert_eq!(p.payload.len(), 320, "G.722 packet size");
        pcm.extend(dec.decode(&p.payload));
    }
    // the RTP clock of G.722 is 8 kHz: 320 bytes advance it by 320
    for w in rx.windows(2) {
        assert_eq!(w[1].ts.wrapping_sub(w[0].ts), 320, "G.722 timestamp step");
    }
    let body = &pcm[6000..pcm.len() - 2000];
    let f = tone_freq(body, 16000.0);
    assert!(
        (f - 6000.0).abs() <= 120.0,
        "tone measured {f:.0} Hz at the phone, want 6000 (the upper band did not get there)"
    );
    assert!(rms(body) >= 0.7 * 8000.0 / 2f64.sqrt());
    session.close();
}

// TestUplinkWideband_PhoneToDeviceIsG722
#[tokio::test]
async fn uplink_wideband_phone_to_device_is_g722() {
    let (session, peer) = connected_pair(g722_peer()).await;
    peer.tone(true, 5000.0, 20, 8000.0, 3.0);
    let out = session.audio_out();
    let mut got: Vec<i16> = Vec::new();
    let start = Instant::now();
    while start.elapsed() < Duration::from_millis(3300) {
        if let Ok(Some(f)) = tokio::time::timeout(Duration::from_millis(100), async {
            out.lock().await.recv().await
        })
        .await
        {
            got.extend(decode_l16_frame(&f));
        }
    }
    assert!(
        (40000..=54000).contains(&got.len()),
        "{} samples came out of 3 s of audio, want about 48000",
        got.len()
    );
    let body = &got[8000..got.len() - 4000];
    let f = tone_freq(body, 16000.0);
    assert!(
        (f - 5000.0).abs() <= 100.0,
        "uplink tone measured {f:.0} Hz at the device, want 5000"
    );
    assert!(rms(body) >= 0.7 * 8000.0 / 2f64.sqrt());
    assert_eq!(session.uplink_codec(), Some(PhoneCodec::G722));
    session.close();
}

// A peer that negotiated G.722/PCMU under DYNAMIC payload types: Go answered it and then dropped
// every packet as foreign (PARITY.md, 4a oddities). The gate is built from the negotiated types.
#[tokio::test]
async fn a_peer_on_dynamic_payload_types_is_heard() {
    let o = PeerOpts {
        g722: true,
        pcmu: true,
        dynamic: Some((96, 97)),
        ..PeerOpts::default()
    };
    let (session, peer) = connected_pair(o).await;
    let n = session.negotiated();
    assert_eq!(
        (n.g722_pt, n.pcmu_pt, n.send),
        (Some(96), Some(97), Some(PhoneCodec::G722))
    );
    peer.tone(true, 1000.0, 20, 8000.0, 1.5);
    let out = session.audio_out();
    let mut got: Vec<i16> = Vec::new();
    let start = Instant::now();
    while start.elapsed() < Duration::from_millis(1800) {
        if let Ok(Some(f)) = tokio::time::timeout(Duration::from_millis(100), async {
            out.lock().await.recv().await
        })
        .await
        {
            got.extend(decode_l16_frame(&f));
        }
    }
    assert_eq!(session.uplink_codec(), Some(PhoneCodec::G722));
    let f = tone_freq(&got[6000..got.len() - 3000], 16000.0);
    assert!((f - 1000.0).abs() <= 30.0, "tone measured {f:.0} Hz");
    // and the device's audio goes out under the negotiated type
    session
        .audio_in()
        .send(encode_l16_frame(&vec![3000i16; 640]))
        .await
        .unwrap();
    sleep(Duration::from_millis(300)).await;
    assert!(peer.received().iter().any(|p| p.pt == 96));
    session.close();
}

/// A 20 s G.722 5 kHz uplink soak (the interop runs saw an isolated 256 ms window now and then
/// whose dominant frequency was not the tone): with a perfectly paced str0m peer every window of
/// the device-side audio must be the tone.
#[tokio::test]
async fn uplink_wideband_soak_has_no_glitch_windows() {
    let (session, peer) = connected_pair(g722_peer()).await;
    peer.tone(true, 5000.0, 20, 8000.0, 20.0);
    let out = session.audio_out();
    let mut got: Vec<i16> = Vec::new();
    let start = Instant::now();
    while start.elapsed() < Duration::from_millis(20500) {
        if let Ok(Some(f)) = tokio::time::timeout(Duration::from_millis(100), async {
            out.lock().await.recv().await
        })
        .await
        {
            got.extend(decode_l16_frame(&f));
        }
    }
    let body = &got[16000..got.len() - 16000];
    let (mut bad, mut n) = (0, 0);
    for w in body.as_chunks::<4096>().0 {
        n += 1;
        let pcm = encode_l16_frame(w.as_slice());
        let (t, o) = (
            common::goertzel(&pcm, 16000.0, 5000.0),
            common::goertzel(&pcm, 16000.0, 1000.0),
        );
        if t < 20.0 * o {
            bad += 1;
        }
    }
    eprintln!("soak: {bad} glitch windows of {n}");
    assert_eq!(bad, 0, "{bad} of {n} windows were not the tone");
    session.close();
}
