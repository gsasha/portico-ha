//! `joinCallToBrowser` against a real `gate_sip_server` driver and a fake station on loopback
//! (`serveranswer_test.go`), with a fake browser leg, and then with a REAL str0m session and a
//! str0m peer as the browser: the whole path station -> transport -> browser. Nothing here can
//! reach anything but 127.0.0.1.

mod common;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use common::peer::{Peer, PeerOpts};
use common::*;
use tokio::sync::mpsc;
use transport_rs::call::ClientLeg;
use transport_rs::driver::Driver;
use transport_rs::sfu::encode_l16_frame;
use transport_rs::sfu::session::{Session, SessionConfig};
use transport_rs::webrtc::join_call_to_browser;

/// `fakeLeg` (the main_test.go helper): the test fires connected and disconnected.
struct FakeLeg {
    on_conn: Mutex<Option<Box<dyn FnOnce() + Send>>>,
    on_disc: Mutex<Vec<Box<dyn FnOnce() + Send>>>,
    connected: AtomicBool,
    gone: AtomicBool,
    in_tx: mpsc::Sender<Vec<u8>>,
    pub in_rx: Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>>,
    pub out_tx: mpsc::Sender<Vec<u8>>,
    out_rx: Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>>,
}

impl FakeLeg {
    fn new() -> Arc<FakeLeg> {
        let (in_tx, in_rx) = mpsc::channel(8);
        let (out_tx, out_rx) = mpsc::channel(8);
        Arc::new(FakeLeg {
            on_conn: Mutex::new(None),
            on_disc: Mutex::new(Vec::new()),
            connected: AtomicBool::new(false),
            gone: AtomicBool::new(false),
            in_tx,
            in_rx: Arc::new(tokio::sync::Mutex::new(in_rx)),
            out_tx,
            out_rx: Arc::new(tokio::sync::Mutex::new(out_rx)),
        })
    }
    fn connect(&self) {
        self.connected.store(true, Ordering::SeqCst);
        if let Some(cb) = self.on_conn.lock().unwrap().take() {
            cb();
        }
    }
    fn disconnect(&self) {
        self.gone.store(true, Ordering::SeqCst);
        let cbs: Vec<_> = self.on_disc.lock().unwrap().drain(..).collect();
        for cb in cbs {
            cb();
        }
    }
}

impl ClientLeg for FakeLeg {
    fn on_connected(&self, cb: Box<dyn FnOnce() + Send>) {
        if self.connected.load(Ordering::SeqCst) {
            cb();
        } else {
            *self.on_conn.lock().unwrap() = Some(cb);
        }
    }
    fn on_disconnected(&self, cb: Box<dyn FnOnce() + Send>) {
        if self.gone.load(Ordering::SeqCst) {
            cb();
        } else {
            self.on_disc.lock().unwrap().push(cb);
        }
    }
    fn input(&self) -> mpsc::Sender<Vec<u8>> {
        self.in_tx.clone()
    }
    fn output(&self) -> Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>> {
        self.out_rx.clone()
    }
}

fn drivers(h: &Harness) -> Vec<Arc<dyn Driver>> {
    vec![h.d.clone() as Arc<dyn Driver>]
}

// TestServerRingNeverAnsweredBeforeTheBrowserConnects
#[tokio::test]
async fn server_ring_never_answered_before_the_browser_connects() {
    let h = Harness::with(Opts::ring(5000)).await;
    register_station(&h).await;
    h.st.invite("call-w3-1", 1, &h.st.sdp("wide")).await;
    assert!(has(&h.st.collect(ms(300)).await, "180"), "no ring");
    let leg = FakeLeg::new();
    join_call_to_browser(&h.ctx, &drivers(&h), "call-w3-1", leg.clone());
    assert!(
        !has(&h.st.collect(ms(500)).await, "200"),
        "200 sent before the browser connected"
    );
    assert!(
        transport_rs::driver::ConnectAnswerer::ringing_call(&*h.d, "call-w3-1"),
        "a browser that never connected must leave the ring answerable"
    );
    leg.disconnect(); // it never connected and never answered: hangs up nothing
    let got = h.st.collect(ms(200)).await;
    assert!(
        !has(&got, "BYE") && !has(&got, "200"),
        "a leg that never connected touched the call"
    );
    assert!(
        transport_rs::driver::ConnectAnswerer::ringing_call(&*h.d, "call-w3-1"),
        "ring lost"
    );
}

// TestServerRingBrowserConnectsAudioBothWaysThenLeavesHangsUp
#[tokio::test]
async fn server_ring_browser_connects_audio_both_ways_then_leaves_hangs_up() {
    let h = Harness::new().await;
    register_station(&h).await;
    h.st.invite("call-w3-2", 1, &h.st.sdp("wide")).await;
    h.st.collect(ms(200)).await;
    let leg = FakeLeg::new();
    join_call_to_browser(&h.ctx, &drivers(&h), "call-w3-2", leg.clone());
    leg.connect();
    assert!(
        has(&h.st.collect(ms(800)).await, "200"),
        "no 200 after the browser connected"
    );
    // station -> browser (1280-byte frames, 16 kHz L16)
    let pcm: Vec<u8> = (0..640).map(|i| (i % 7 + 1) as u8).collect();
    for i in 1..=2u16 {
        h.st.rtp
            .send_to(&rtp_packet(96, i, u32::from(i) * 320, &pcm), h.rtp_addr())
            .await
            .unwrap();
    }
    let f = tokio::time::timeout(ms(2000), async { leg.in_rx.lock().await.recv().await })
        .await
        .expect("station audio never reached the browser")
        .unwrap();
    assert_eq!(f.len(), 1280);
    // browser -> station
    let b: Vec<u8> = (0..1280).map(|i| (i % 5 + 1) as u8).collect();
    leg.out_tx.send(b).await.unwrap();
    let mut heard = false;
    for _ in 0..100 {
        if let Some((_, pl)) = h.st.read_rtp(ms(50)).await {
            if pl.iter().any(|x| *x != 0) {
                heard = true;
                break;
            }
        }
    }
    assert!(heard, "browser audio never reached the station");
    h.st.collect(ms(50)).await;
    leg.disconnect();
    assert!(
        has(&h.st.collect(ms(1000)).await, "BYE"),
        "the browser left and the call was not hung up"
    );
}

// TestServerRingSecondBrowserIsRefusedAndHangsUpNothing
#[tokio::test]
async fn server_ring_second_browser_is_refused_and_hangs_up_nothing() {
    let h = Harness::new().await;
    register_station(&h).await;
    h.st.invite("call-w3-3", 1, &h.st.sdp("wide")).await;
    h.st.collect(ms(200)).await;
    let (a, b) = (FakeLeg::new(), FakeLeg::new());
    join_call_to_browser(&h.ctx, &drivers(&h), "call-w3-3", a.clone());
    join_call_to_browser(&h.ctx, &drivers(&h), "call-w3-3", b.clone());
    a.connect();
    assert!(
        has(&h.st.collect(ms(600)).await, "200"),
        "first answerer got no 200"
    );
    b.connect(); // loses
    sleep_ms(200).await;
    b.disconnect();
    assert!(
        !has(&h.st.collect(ms(400)).await, "BYE"),
        "the refused answerer's leaving hung the call up"
    );
    // a late third joiner finds nothing to join (the call is held, not ringing)
    let c = FakeLeg::new();
    join_call_to_browser(&h.ctx, &drivers(&h), "call-w3-3", c.clone());
    c.connect();
    c.disconnect();
    assert!(
        !has(&h.st.collect(ms(300)).await, "BYE"),
        "a late joiner disturbed the call"
    );
    a.disconnect(); // the owner leaving ends it
    assert!(
        has(&h.st.collect(ms(1000)).await, "BYE"),
        "owner left, no BYE"
    );
}

/// The browser leaves while the answer is going out: the call is hung up again once it is in.
#[tokio::test]
async fn a_browser_that_leaves_right_after_connecting_does_not_leave_the_call_up() {
    let h = Harness::new().await;
    register_station(&h).await;
    h.st.invite("call-w3-4", 1, &h.st.sdp("wide")).await;
    h.st.collect(ms(200)).await;
    let leg = FakeLeg::new();
    join_call_to_browser(&h.ctx, &drivers(&h), "call-w3-4", leg.clone());
    leg.connect();
    leg.disconnect();
    let got = h.st.collect(ms(1200)).await;
    // the INVITE got a final answer (R2-90e: "no 200" alone also passes when the join did
    // nothing at all), and a 200 that went out was followed by the BYE
    assert!(
        got.iter().any(|m| m.code >= 200),
        "the INVITE was never answered: {:?}",
        codes(&got)
    );
    assert!(!has(&got, "200") || has(&got, "BYE"), "{:?}", codes(&got));
}

/// The whole path with a REAL browser leg: station -> driver -> session -> str0m peer and back.
#[tokio::test]
async fn station_to_browser_both_ways_through_a_real_session() {
    let h = Harness::new().await;
    register_station(&h).await;
    h.st.invite("call-real-1", 1, &h.st.sdp("wide")).await;
    assert!(has(&h.st.collect(ms(300)).await, "180"));
    let peer = Peer::new(PeerOpts {
        g722: true,
        pcmu: true,
        ..PeerOpts::default()
    })
    .await;
    let session = Session::with_config("call-real-1", SessionConfig::loopback()).unwrap();
    let answer = session.handle_offer(&peer.offer().await).await.unwrap();
    join_call_to_browser(&h.ctx, &drivers(&h), "call-real-1", session.clone());
    // nothing is answered until the session is connected
    assert!(
        !has(&h.st.collect(ms(300)).await, "200"),
        "answered before the browser connected"
    );
    peer.accept_answer(&answer).await.unwrap();
    assert!(peer.wait_connected(10).await);
    assert!(
        has(&h.st.collect(ms(1500)).await, "200"),
        "no 200 once the browser connected"
    );
    h.st.ack("call-real-1").await;

    // station -> browser: 2 s of a 440 Hz tone as 20 ms RTP of 16 kHz L16 (type 96); browser ->
    // station: a 1 kHz G.722 tone. The station's RTP socket both sends and listens.
    peer.tone(true, 1000.0, 20, 8000.0, 2.0);
    let mut heard: Vec<u8> = Vec::new();
    let mut tk = tokio::time::interval(ms(20));
    let (mut ph, mut i) = (0.0f64, 0u16);
    let mut buf = vec![0u8; 4096];
    let end = tokio::time::Instant::now() + ms(2600);
    let to = h.rtp_addr();
    loop {
        tokio::select! {
            _ = tk.tick(), if i < 100 => {
                let pcm: Vec<i16> = (0..320)
                    .map(|_| {
                        let v = (8000.0 * ph.sin()) as i16;
                        ph += 2.0 * std::f64::consts::PI * 440.0 / 16000.0;
                        v
                    })
                    .collect();
                let _ = h.st.rtp.send_to(&rtp_packet(96, i, u32::from(i) * 320, &encode_l16_frame(&pcm)), to).await;
                i += 1;
            }
            r = h.st.rtp.recv_from(&mut buf) => {
                if let Ok((n, _)) = r {
                    if n > 12 {
                        heard.extend_from_slice(&buf[12..n]);
                    }
                }
            }
            _ = tokio::time::sleep_until(end) => break,
        }
    }
    // station -> browser: G.722 reached the peer, decodes to the 440 Hz tone
    let rx = peer.received();
    assert!(
        rx.len() > 30,
        "only {} packets reached the browser",
        rx.len()
    );
    assert!(
        rx.iter().all(|p| p.pt == 9),
        "the browser offered G.722 first and must be sent it"
    );
    let mut dec = transport_rs::sfu::G722Decoder::new();
    let pcm: Vec<i16> = rx.iter().flat_map(|p| dec.decode(&p.payload)).collect();
    let body = &pcm[4000..pcm.len() - 2000];
    let pcm_bytes = encode_l16_frame(body);
    let (p440, p1k) = (
        goertzel(&pcm_bytes, 16000.0, 440.0),
        goertzel(&pcm_bytes, 16000.0, 1000.0),
    );
    assert!(
        p440 > 20.0 * p1k,
        "the station's 440 Hz tone did not arrive: {p440} vs {p1k}"
    );
    // browser -> station: the 1 kHz tone arrived as L16 at the station
    assert!(
        heard.len() > 20000,
        "the station heard {} bytes",
        heard.len()
    );
    let (b1k, b440) = (
        goertzel(&heard[8000..], 16000.0, 1000.0),
        goertzel(&heard[8000..], 16000.0, 440.0),
    );
    assert!(
        b1k > 20.0 * b440,
        "the browser's 1 kHz tone did not arrive: {b1k} vs {b440}"
    );

    // the browser leaves: the station is hung up
    h.st.collect(ms(10)).await;
    peer.close();
    assert!(
        has(&h.st.collect(ms(3000)).await, "BYE"),
        "the browser left and the station was not hung up"
    );
    session.close();
}

/// A panic in the session's RTP path still tears the call down: the station gets its BYE.
#[tokio::test]
async fn a_panicked_session_still_hangs_the_station_up() {
    let h = Harness::new().await;
    register_station(&h).await;
    h.st.invite("call-panic-1", 1, &h.st.sdp("wide")).await;
    assert!(has(&h.st.collect(ms(300)).await, "180"));
    let peer = Peer::new(PeerOpts {
        g722: true,
        pcmu: true,
        ..PeerOpts::default()
    })
    .await;
    let cfg = SessionConfig {
        fault: transport_rs::sfu::session::Fault::PanicInRtp,
        ..SessionConfig::loopback()
    };
    let session = Session::with_config("call-panic-1", cfg).unwrap();
    let answer = session.handle_offer(&peer.offer().await).await.unwrap();
    join_call_to_browser(&h.ctx, &drivers(&h), "call-panic-1", session.clone());
    peer.accept_answer(&answer).await.unwrap();
    assert!(peer.wait_connected(10).await);
    assert!(has(&h.st.collect(ms(1500)).await, "200"));
    peer.tone(true, 1000.0, 20, 8000.0, 2.0); // the first RTP packet panics the session
    assert!(
        has(&h.st.collect(ms(4000)).await, "BYE"),
        "no BYE after the session panicked"
    );
}
