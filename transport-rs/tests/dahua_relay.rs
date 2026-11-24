//! The device leg of the relay (`internal/call/relay.go`) and the pending-ring bridge of
//! `main.go`, against a fake station on loopback: `relay_test.go`, `relay_host_test.go`, the
//! `findPendingRing` tests of `main_test.go` and `two_doors_test.go`, a relay joined to a fake
//! browser leg through the real `dahua_sip` driver (`TestAnswerViaRelayOnceConnected` against a
//! real SIP relay), and the whole call over the IPC with a str0m peer as the browser. The Go
//! name is in a comment on each test. Nothing here reaches anything but 127.0.0.1.

mod common;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::dahua::*;
use common::peer::{Peer, PeerOpts};
use serde_json::{json, Map, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use transport_rs::call::{ClientLeg, Relay};
use transport_rs::cancel::CancelToken;
use transport_rs::driver::dahua::Dahua;
use transport_rs::driver::{
    BoxFuture, Capabilities, Driver, DriverError, EventSink, MediaTarget, PendingRinger,
};
use transport_rs::sfu::session::SessionConfig;
use transport_rs::sip::{
    build_sdp, parse_request, parse_rtp_packet, parse_sdp, Endpoint, IncomingRequest, RtpPacket,
    RTP_FRAME_BYTES,
};
use transport_rs::webrtc::{
    find_pending_ring, join_call_to_browser_with, RelayClosingSink, RelayRegistry,
};
use transport_rs::{dispatch, ipc};

use tokio::sync::mpsc;

fn ms(n: u64) -> Duration {
    Duration::from_millis(n)
}

async fn endpoint_to(st: &DStation) -> Endpoint {
    Endpoint::new("127.0.0.1", st.port() as i64).await.unwrap()
}

// ---- the device leg ------------------------------------------------------------------------

// TestRelay_DialDeviceExchangesRealRTPAudio: DialDevice places a real INVITE against a fake
// device, negotiates the SDP, opens a real audio session, and genuine RTP flows both ways.
#[tokio::test]
async fn relay_dial_device_exchanges_real_rtp_audio() {
    let st = DStation::with("ok", "200").await;
    let ep = endpoint_to(&st).await;
    let relay = Relay::new();
    relay
        .dial_device(&ep, "portico", "9901#0", "127.0.0.1")
        .await
        .unwrap();
    assert!(
        !relay.call_id().is_empty(),
        "Relay.CallID is empty after a successful DialDevice"
    );
    // the port the offer advertised is the port the relay listens on
    assert!(
        st.wait_for("INVITE", ms(2000)).await,
        "the fake device never saw our INVITE"
    );
    let offered = st.media_port();
    assert_ne!(offered, 0);
    let leg = relay.device_leg().unwrap();

    let device_frame = vec![0x33u8; RTP_FRAME_BYTES];
    let pkt = RtpPacket {
        payload_type: 96,
        payload: device_frame.clone(),
        ..Default::default()
    }
    .marshal();
    st.rtp
        .send_to(&pkt, format!("127.0.0.1:{offered}"))
        .await
        .unwrap();
    let got = tokio::time::timeout(ms(2000), async { leg.output.lock().await.recv().await })
        .await
        .expect("Relay.AudioOut never received the device's frame")
        .unwrap();
    assert_eq!(got, device_frame);

    let ours = vec![0x44u8; RTP_FRAME_BYTES];
    leg.input.send(ours.clone()).await.unwrap();
    let mut buf = vec![0u8; 2048];
    let (n, _) = tokio::time::timeout(ms(2000), st.rtp.recv_from(&mut buf))
        .await
        .expect("device never received our RTP frame")
        .unwrap();
    assert_eq!(parse_rtp_packet(&buf[..n]).unwrap().payload, ours);
    relay.close().await.unwrap();
    let _ = ep.close();
}

fn offer_sdp(m: &[String]) -> String {
    let inv = m
        .iter()
        .find(|x| x.starts_with("INVITE "))
        .expect("no INVITE");
    inv.split_once("\r\n\r\n").unwrap().1.to_string()
}

// TestDialDevice_WithoutAConfiguredHostAdvertisesTheRoutedAddress
#[tokio::test]
async fn dial_device_without_a_configured_host_advertises_the_routed_address() {
    let st = DStation::with("ok", "200").await;
    let ep = endpoint_to(&st).await;
    let relay = Relay::new();
    relay
        .dial_device(&ep, "portico", "9901#0", "")
        .await
        .expect("DialDevice with no -host");
    let body = offer_sdp(&st.collect(ms(300)).await);
    let info = parse_sdp(body.as_bytes()).unwrap();
    assert!(info.host == "127.0.0.1" && info.port != 0, "{body}");
    assert!(
        !body.contains("IN IP4 \r\n"),
        "empty host in the SDP:\n{body}"
    );
    relay.close().await.unwrap();
}

// TestDialDevice_AConfiguredHostIsAdvertisedAsIs
#[tokio::test]
async fn dial_device_with_a_configured_host_advertises_it_as_is() {
    let st = DStation::with("ok", "200").await;
    let ep = endpoint_to(&st).await;
    let relay = Relay::new();
    relay
        .dial_device(&ep, "portico", "9901#0", "192.168.2.77")
        .await
        .unwrap();
    let body = offer_sdp(&st.collect(ms(300)).await);
    assert!(
        body.contains("c=IN IP4 192.168.2.77\r\n"),
        "configured host not advertised:\n{body}"
    );
    relay.close().await.unwrap();
}

// TestDialDevice_RefusesAnUnusableHostAndSendsNothing
#[tokio::test]
async fn dial_device_refuses_an_unusable_host_and_sends_nothing() {
    let st = DStation::with("ok", "200").await;
    let ep = endpoint_to(&st).await;
    for bad in ["0.0.0.0", "not-an-ip"] {
        let relay = Relay::new();
        assert!(
            relay
                .dial_device(&ep, "portico", "9901#0", bad)
                .await
                .is_err(),
            "host {bad:?}"
        );
        assert!(
            relay.call_id().is_empty() && relay.device_leg().is_none(),
            "a refused dial left state behind"
        );
    }
    assert!(
        st.collect(ms(300)).await.is_empty(),
        "an INVITE was sent despite the refusal"
    );
}

// TestAnswerDevice_WithNoHostAndNoRouteFailsLoudly: a ring whose request has no endpoint to read
// an address from, and no -host.
#[tokio::test]
async fn answer_device_with_no_host_and_no_route_fails_loudly() {
    let req = IncomingRequest::detached(
        parse_request(b"INVITE sip:x SIP/2.0\r\nCall-ID: x\r\n\r\n").unwrap(),
    );
    let relay = Relay::new();
    let e = relay.answer_device(&req, "").await.unwrap_err();
    assert!(e.contains("no media host"), "{e}");
    assert!(
        relay.device_leg().is_none(),
        "an audio socket was opened for a refused answer"
    );
}

/// A ring addressed to a relay endpoint, as `TestAnswerDevice_WithoutAConfiguredHost...` builds it.
async fn ring_to_endpoint(st: &DStation, ep: &Endpoint, id: &str) -> IncomingRequest {
    let (tx, mut rx) = mpsc::channel(2);
    ep.handle("INVITE", move |r: IncomingRequest| {
        let tx = tx.clone();
        async move {
            let _ = tx.send(r).await;
        }
    });
    let ra = st.rtp.local_addr().unwrap();
    let offer = build_sdp(&ra.ip().to_string(), ra.port() as i64);
    let invite = format!(
        "INVITE sip:9901#0@{} SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKhost1\r\n\
         From: <sip:9901#0@127.0.0.1>;tag=devicetag\r\nTo: <sip:9901#0@{}>\r\nCall-ID: {id}\r\nCSeq: 1 INVITE\r\n\
         Content-Type: application/sdp\r\nContent-Length: {}\r\n\r\n{offer}",
        ep.local_addr(),
        ep.local_addr(),
        offer.len()
    );
    st.sip
        .send_to(invite.as_bytes(), ep.local_addr())
        .await
        .unwrap();
    tokio::time::timeout(ms(2000), rx.recv())
        .await
        .expect("the ring never arrived")
        .unwrap()
}

// TestAnswerDevice_WithoutAConfiguredHostAdvertisesTheRingsLocalAddress
#[tokio::test]
async fn answer_device_without_a_configured_host_advertises_the_rings_local_address() {
    let st = DStation::new().await;
    let ep = endpoint_to(&st).await;
    // the endpoint talks to the station; make the station's first datagram known
    ep.write(b"\r\n\r\n").await.ok();
    let req = ring_to_endpoint(&st, &ep, "host-1@127.0.0.1").await;
    assert_eq!(req.local_host(), "127.0.0.1");
    st.collect(ms(50)).await;
    let relay = Relay::new();
    relay
        .answer_device(&req, "")
        .await
        .expect("AnswerDevice with no -host");
    assert!(st.wait_for("SIP/2.0 200", ms(2000)).await, "no 200 OK");
    let ok = st
        .collect(ms(50))
        .await
        .into_iter()
        .find(|m| m.starts_with("SIP/2.0 200"))
        .unwrap();
    assert!(
        ok.contains("c=IN IP4 127.0.0.1\r\n"),
        "200 OK SDP lacks the ring's local address:\n{ok}"
    );
    assert_eq!(relay.call_id(), "host-1@127.0.0.1");
    relay.close().await.unwrap();
}

// ---- Rust only: the relay's own guarantees ------------------------------------------------------

/// Go sends a second BYE when `Close` is called twice; this relay closes once (PARITY.md 100).
#[tokio::test]
async fn close_is_idempotent_and_sends_one_bye() {
    let st = DStation::with("ok", "200").await;
    let ep = endpoint_to(&st).await;
    let relay = Arc::new(Relay::new());
    relay
        .dial_device(&ep, "portico", "9901#0", "127.0.0.1")
        .await
        .unwrap();
    let (a, b) = (relay.clone(), relay.clone());
    let (r1, r2) = tokio::join!(a.close(), b.close());
    r1.unwrap();
    r2.unwrap();
    relay.close().await.unwrap();
    let got = st.collect(ms(300)).await;
    assert_eq!(
        got.iter().filter(|m| m.starts_with("BYE ")).count(),
        1,
        "{got:?}"
    );
    assert!(relay.device_leg().is_none() && relay.is_closed());
}

/// The browser left while the SIP exchange was under way: the leg that comes up afterwards is
/// torn down at once (200 then BYE), nothing is left running and the result says so.
#[tokio::test]
async fn a_close_during_the_answer_tears_the_new_leg_down() {
    let st = DStation::new().await;
    let ep = endpoint_to(&st).await;
    ep.write(b"\r\n\r\n").await.ok();
    let req = ring_to_endpoint(&st, &ep, "host-9@127.0.0.1").await;
    st.collect(ms(50)).await;
    let relay = Arc::new(Relay::new());
    relay.close().await.unwrap(); // the disconnect callback ran first
    let e = relay.answer_device(&req, "127.0.0.1").await.unwrap_err();
    assert!(
        e.contains("closed while the device leg was coming up"),
        "{e}"
    );
    assert!(relay.device_leg().is_none());
    let got = st.collect(ms(500)).await;
    assert!(got.iter().any(|m| m.starts_with("SIP/2.0 200")), "{got:?}");
    assert!(
        got.iter().any(|m| m.starts_with("BYE ")),
        "the late leg was not hung up: {got:?}"
    );
}

/// The future is dropped (or its task panics) after the device has its 200 and before the leg is
/// published: the call is hung up all the same (a drop guard owns it in that window).
#[tokio::test]
async fn a_dropped_answer_in_flight_still_hangs_the_call_up() {
    let st = DStation::new().await;
    let ep = endpoint_to(&st).await;
    ep.write(b"\r\n\r\n").await.ok();
    let req = ring_to_endpoint(&st, &ep, "host-5@127.0.0.1").await;
    st.collect(ms(50)).await;
    let relay = Arc::new(Relay::new());
    let gate = Arc::new(tokio::sync::Notify::new());
    relay.hold_install_for_test(gate);
    let (r2, q) = (relay.clone(), req.clone());
    let task = tokio::spawn(async move { r2.answer_device(&q, "127.0.0.1").await });
    assert!(
        st.wait_for("SIP/2.0 200", ms(2000)).await,
        "the answer never went out"
    );
    task.abort(); // the browser's task is gone
    let _ = task.await;
    assert!(
        st.wait_for("BYE ", ms(3000)).await,
        "the answered call was left up"
    );
    assert!(relay.device_leg().is_none() && !relay.is_up());
}

/// A device that refuses the INVITE leaves no socket and no state behind.
#[tokio::test]
async fn a_refused_dial_leaves_nothing_behind() {
    let st = DStation::with("ok", "486").await;
    let ep = endpoint_to(&st).await;
    let relay = Relay::new();
    let e = relay
        .dial_device(&ep, "portico", "9901#0", "192.168.2.77")
        .await
        .unwrap_err();
    assert_eq!(
        e,
        "call: dialing device leg: sip: INVITE failed: 486 Busy Here"
    );
    assert!(relay.device_leg().is_none() && relay.call_id().is_empty() && !relay.is_up());
}

// ---- findPendingRing and the two doors ----------------------------------------------------------

/// `pendingRingFake`: a driver holding one pending ring, like `dahua.Driver`.
struct RingFake {
    ring: Mutex<Option<IncomingRequest>>,
    call_id: String,
}

impl RingFake {
    fn new(id: &str) -> Arc<RingFake> {
        let raw = format!("INVITE sip:x SIP/2.0\r\nCall-ID: {id}\r\n\r\n");
        Arc::new(RingFake {
            ring: Mutex::new(Some(IncomingRequest::detached(
                parse_request(raw.as_bytes()).unwrap(),
            ))),
            call_id: id.to_string(),
        })
    }
    fn has(&self) -> bool {
        self.ring.lock().unwrap().is_some()
    }
}

impl Driver for RingFake {
    fn configure(&mut self, _: &Map<String, Value>) -> Result<(), DriverError> {
        Ok(())
    }
    fn capabilities(&self) -> Capabilities {
        Capabilities::default()
    }
    fn start<'a>(
        &'a self,
        _: &'a CancelToken,
        _: Arc<dyn EventSink>,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async { Ok(()) })
    }
    fn stop(&self) -> BoxFuture<'_, Result<(), DriverError>> {
        Box::pin(async { Ok(()) })
    }
    fn dial<'a>(
        &'a self,
        _: &'a CancelToken,
        _: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async { Ok(()) })
    }
    fn answer<'a>(
        &'a self,
        _: &'a CancelToken,
        _: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async { Ok(()) })
    }
    fn open_relay<'a>(&'a self, _: &'a CancelToken) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async { Ok(()) })
    }
    fn as_pending_ringer(&self) -> Option<&dyn PendingRinger> {
        Some(self)
    }
}

impl PendingRinger for RingFake {
    fn pending_ring(&self) -> Option<IncomingRequest> {
        self.ring.lock().unwrap().take()
    }
    fn pending_ring_for(&self, call_id: &str) -> Option<IncomingRequest> {
        let mut g = self.ring.lock().unwrap();
        if g.is_some() && self.call_id == call_id {
            g.take()
        } else {
            None
        }
    }
}

/// A driver with no `PendingRinger` (`claimant{}`): never matches the type assertion.
struct Plain;
impl Driver for Plain {
    fn configure(&mut self, _: &Map<String, Value>) -> Result<(), DriverError> {
        Ok(())
    }
    fn capabilities(&self) -> Capabilities {
        Capabilities::default()
    }
    fn start<'a>(
        &'a self,
        _: &'a CancelToken,
        _: Arc<dyn EventSink>,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async { Ok(()) })
    }
    fn stop(&self) -> BoxFuture<'_, Result<(), DriverError>> {
        Box::pin(async { Ok(()) })
    }
    fn dial<'a>(
        &'a self,
        _: &'a CancelToken,
        _: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async { Ok(()) })
    }
    fn answer<'a>(
        &'a self,
        _: &'a CancelToken,
        _: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async { Ok(()) })
    }
    fn open_relay<'a>(&'a self, _: &'a CancelToken) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async { Ok(()) })
    }
}

// TestFindPendingRingMatchesTheCallId
#[test]
fn find_pending_ring_matches_the_call_id() {
    let (a, b) = (RingFake::new("call-a"), RingFake::new("call-b"));
    let drivers: Vec<Arc<dyn Driver>> = vec![a.clone(), b.clone()];
    let got = find_pending_ring(&drivers, "call-b").expect("call-b's ring");
    assert_eq!(got.header("Call-ID"), "call-b");
    assert!(a.has(), "call-a's ring was consumed by an offer for call-b");
    assert!(!b.has(), "call-b's ring was not consumed");
    assert!(
        find_pending_ring(&drivers, "call-zzz").is_none(),
        "an unknown call id matched a ring"
    );
    assert!(a.has(), "an unknown call id consumed call-a's ring");
}

// TestFindPendingRingFallsBackToAnyRingWithoutAnId
#[test]
fn find_pending_ring_falls_back_to_any_ring_without_an_id() {
    let a = RingFake::new("call-a");
    let drivers: Vec<Arc<dyn Driver>> = vec![Arc::new(Plain), a.clone()];
    let got = find_pending_ring(&drivers, "").expect("the only pending ring");
    assert_eq!(got.header("Call-ID"), "call-a");
    let only: Vec<Arc<dyn Driver>> = vec![a];
    assert!(
        find_pending_ring(&only, "").is_none(),
        "the ring was handed out twice"
    );
}

// TestTwoDoors_RingingAtOnceEachOfferTakesItsOwnRingAndTheOtherWaits
#[test]
fn two_doors_ringing_at_once_each_offer_takes_its_own_ring_and_the_other_waits() {
    let (gate, entrance) = (RingFake::new("gate-1"), RingFake::new("door-1"));
    let drivers: Vec<Arc<dyn Driver>> = vec![gate.clone(), entrance.clone()];
    let got = find_pending_ring(&drivers, "door-1").expect("door-1's ring");
    assert_eq!(got.header("Call-ID"), "door-1");
    assert!(
        gate.has(),
        "the gate's ring was consumed by an offer for the entrance's call"
    );
    assert!(
        find_pending_ring(&drivers, "door-1").is_none(),
        "the entrance's ring was handed out twice"
    );
    let got = find_pending_ring(&drivers, "gate-1").expect("gate-1's ring");
    assert_eq!(got.header("Call-ID"), "gate-1");
}

// ---- the relay joined to a browser leg through the real driver ------------------------------------

/// `fakeLeg`: the test fires connected and disconnected.
struct FakeLeg {
    on_conn: Mutex<Option<Box<dyn FnOnce() + Send>>>,
    on_disc: Mutex<Vec<Box<dyn FnOnce() + Send>>>,
    /// called by `input()`, which the relay asks for just before it reports the answer (R3A-4)
    on_input: Mutex<Option<Box<dyn Fn() + Send>>>,
    connected: AtomicBool,
    gone: AtomicBool,
    in_tx: mpsc::Sender<Vec<u8>>,
    in_rx: Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>>,
    out_tx: mpsc::Sender<Vec<u8>>,
    out_rx: Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>>,
}

impl FakeLeg {
    fn new() -> Arc<FakeLeg> {
        let (in_tx, in_rx) = mpsc::channel(8);
        let (out_tx, out_rx) = mpsc::channel(8);
        Arc::new(FakeLeg {
            on_conn: Mutex::new(None),
            on_disc: Mutex::new(Vec::new()),
            on_input: Mutex::new(None),
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
        if let Some(f) = &*self.on_input.lock().unwrap() {
            f();
        }
        self.in_tx.clone()
    }
    fn output(&self) -> Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>> {
        self.out_rx.clone()
    }
}

fn cfg() -> Map<String, Value> {
    serde_json::from_str(r#"{"host":"127.0.0.1","sip_user":"8001#1","sip_password":"pw"}"#).unwrap()
}

struct Rig {
    st: DStation,
    ev: Arc<Rec>,
    d: Arc<Dahua>,
    drivers: Vec<Arc<dyn Driver>>,
    relays: Arc<RelayRegistry>,
    ctx: CancelToken,
}

/// A started `dahua_sip` whose sink is the relay-closing wrapper, as `configure` builds it.
async fn rig() -> Rig {
    let st = DStation::new().await;
    let ev = Rec::new();
    let relays = RelayRegistry::new();
    let mut d = Dahua::default();
    d.set_sip_port(st.port() as i64);
    d.configure(&cfg()).unwrap();
    let d = Arc::new(d);
    let ctx = CancelToken::new();
    let sink: Arc<dyn EventSink> = Arc::new(RelayClosingSink::new(ev.clone(), relays.clone()));
    d.start(&ctx, sink).await.unwrap();
    assert!(st.wait_regs(ms(3000)).await);
    let drivers: Vec<Arc<dyn Driver>> = vec![d.clone()];
    Rig {
        st,
        ev,
        d,
        drivers,
        relays,
        ctx,
    }
}

// TestAnswerViaRelayOnceConnected against a real SIP relay: the device is never answered before
// the browser leg connects, then the audio flows both ways, and the browser leaving hangs the
// station up and frees the relay slot.
#[tokio::test]
async fn the_station_is_answered_only_once_the_browser_connects_then_audio_flows_and_leaving_hangs_up(
) {
    let r = rig().await;
    r.st.send(&invite_media("call-r1", "inv1", r.st.rtp_port()))
        .await;
    assert!(r.st.wait_for("SIP/2.0 180", ms(2000)).await);
    assert!(r.ev.wait("ringing|call-r1", ms(1000)).await);
    let leg = FakeLeg::new();
    join_call_to_browser_with(
        &r.ctx,
        &r.drivers,
        &r.relays,
        "127.0.0.1",
        "call-r1",
        leg.clone(),
    );
    assert!(
        r.d.peek_pending_ring().is_none(),
        "the offer did not take the ring"
    );
    let early = r.st.collect(ms(500)).await;
    assert!(
        !early.iter().any(|m| m.starts_with("SIP/2.0 200")),
        "200 sent before the browser connected"
    );
    leg.connect();
    assert!(
        r.st.wait_for("SIP/2.0 200", ms(3000)).await,
        "no 200 once the browser connected"
    );
    let ok =
        r.st.collect(ms(50))
            .await
            .into_iter()
            .find(|m| m.starts_with("SIP/2.0 200"))
            .unwrap();
    let relay_port = parse_sdp(ok.split_once("\r\n\r\n").unwrap().1.as_bytes())
        .unwrap()
        .port as u16;
    // the relay is registered once its answer is in
    let end = tokio::time::Instant::now() + ms(2000);
    while r.relays.is_empty() && tokio::time::Instant::now() < end {
        tokio::time::sleep(ms(10)).await;
    }
    assert_eq!(r.relays.len(), 1);

    // station -> browser
    let frame = vec![0x21u8; RTP_FRAME_BYTES];
    let pkt = RtpPacket {
        payload_type: 96,
        payload: frame.clone(),
        ..Default::default()
    }
    .marshal();
    r.st.rtp
        .send_to(&pkt, format!("127.0.0.1:{relay_port}"))
        .await
        .unwrap();
    let got = tokio::time::timeout(ms(3000), async { leg.in_rx.lock().await.recv().await })
        .await
        .expect("the browser heard nothing")
        .unwrap();
    assert_eq!(got, frame);
    // browser -> station
    let ours = vec![0x42u8; RTP_FRAME_BYTES];
    leg.out_tx.send(ours.clone()).await.unwrap();
    let mut buf = vec![0u8; 2048];
    let (n, _) = tokio::time::timeout(ms(3000), r.st.rtp.recv_from(&mut buf))
        .await
        .expect("the station heard nothing")
        .unwrap();
    assert_eq!(parse_rtp_packet(&buf[..n]).unwrap().payload, ours);

    // the browser leaves: BYE to the station, the slot is freed
    leg.disconnect();
    assert!(
        r.st.wait_for("BYE ", ms(3000)).await,
        "the station was not hung up"
    );
    assert!(r.relays.is_empty());
    r.d.stop().await.unwrap();
}

#[tokio::test]
async fn a_browser_that_never_connects_leaves_nothing_answered() {
    let r = rig().await;
    r.st.send(&invite("call-r2", "inv2", true)).await;
    assert!(r.ev.wait("ringing|call-r2", ms(2000)).await);
    let leg = FakeLeg::new();
    join_call_to_browser_with(
        &r.ctx,
        &r.drivers,
        &r.relays,
        "127.0.0.1",
        "call-r2",
        leg.clone(),
    );
    leg.disconnect(); // gone before it ever connected
    let got = r.st.collect(ms(600)).await;
    assert!(
        !got.iter()
            .any(|m| m.starts_with("SIP/2.0 200") || m.starts_with("BYE ")),
        "{got:?}"
    );
    assert!(r.relays.is_empty());
    r.d.stop().await.unwrap();
}

/// R3A-4, R2-81 (Go's `gone` flag): the browser leaves in the gap between the device answer being
/// complete (the relay reports it, which wakes the task that registers the relay) and that task
/// running. The leg's `input()`, asked for just before the report, starts a task that disconnects: it
/// runs before the registering task, whose own `close` task is not yet run, so `relay.is_closed()` is
/// still false and only the flag the disconnect callback set tells the registering task. No relay is
/// registered, the station is hung up. Mutation: the `gone.store(true, ..)` of `relay_pending_ring`
/// deleted: the dead relay is registered.
#[tokio::test]
async fn a_browser_that_leaves_just_as_the_answer_completes_registers_no_relay() {
    let r = rig().await;
    r.st.send(&invite_media("call-r9", "inv9", r.st.rtp_port()))
        .await;
    assert!(r.ev.wait("ringing|call-r9", ms(2000)).await);
    let leg = FakeLeg::new();
    let l2 = leg.clone();
    *leg.on_input.lock().unwrap() = Some(Box::new(move || {
        let l = l2.clone();
        tokio::spawn(async move { l.disconnect() });
    }));
    join_call_to_browser_with(
        &r.ctx,
        &r.drivers,
        &r.relays,
        "127.0.0.1",
        "call-r9",
        leg.clone(),
    );
    leg.connect();
    assert!(
        r.st.wait_for("BYE ", ms(3000)).await,
        "the station was not hung up"
    );
    tokio::time::sleep(ms(200)).await;
    assert!(
        r.relays.is_empty(),
        "a relay whose browser had left was registered"
    );
    r.d.stop().await.unwrap();
}

/// The station's BYE ends the call: `ended` is reported, the joined relay is closed (its audio
/// session stops, a BYE of ours follows) and forgotten (the relay-closing wrapper).
#[tokio::test]
async fn the_stations_bye_closes_and_forgets_the_relay() {
    let r = rig().await;
    r.st.send(&invite("call-r3", "inv3", true)).await;
    assert!(r.ev.wait("ringing|call-r3", ms(2000)).await);
    let leg = FakeLeg::new();
    join_call_to_browser_with(
        &r.ctx,
        &r.drivers,
        &r.relays,
        "127.0.0.1",
        "call-r3",
        leg.clone(),
    );
    leg.connect();
    assert!(r.st.wait_for("SIP/2.0 200", ms(3000)).await);
    let end = tokio::time::Instant::now() + ms(2000);
    while r.relays.is_empty() && tokio::time::Instant::now() < end {
        tokio::time::sleep(ms(10)).await;
    }
    assert_eq!(r.relays.len(), 1);
    r.st.collect(ms(10)).await;
    r.st.send(&bye("call-r3", "bye3")).await;
    assert!(r.ev.wait("state|call-r3|ended", ms(2000)).await);
    assert!(
        r.relays.is_empty(),
        "the ended call's relay is still registered"
    );
    // the relay closed itself with a BYE of its own (Go does the same)
    assert!(r.st.wait_for("BYE ", ms(3000)).await);
    r.d.stop().await.unwrap();
}

/// Difference 104: the station's BYE ends the call and `ended` is forwarded at once, although our
/// own BYE (the relay's close) is never answered; Go's wrapper waits for the BYE's answer first.
#[tokio::test]
async fn ended_is_forwarded_at_once_although_the_relays_bye_is_never_answered() {
    let r = rig().await;
    r.st.send(&invite_media("call-r5", "inv5", r.st.rtp_port()))
        .await;
    assert!(r.ev.wait("ringing|call-r5", ms(2000)).await);
    let leg = FakeLeg::new();
    join_call_to_browser_with(
        &r.ctx,
        &r.drivers,
        &r.relays,
        "127.0.0.1",
        "call-r5",
        leg.clone(),
    );
    leg.connect();
    assert!(r.st.wait_for("SIP/2.0 200", ms(3000)).await);
    let end = tokio::time::Instant::now() + ms(2000);
    while r.relays.is_empty() && tokio::time::Instant::now() < end {
        tokio::time::sleep(ms(10)).await;
    }
    r.st.set_bye_silent(true);
    r.st.send(&bye("call-r5", "bye5")).await;
    assert!(
        r.ev.wait("state|call-r5|ended", ms(500)).await,
        "ended waited for the relay's BYE"
    );
    assert!(r.relays.is_empty());
    assert!(
        r.st.wait_for("BYE ", ms(2000)).await,
        "the relay was not closed"
    );
    r.d.stop().await.unwrap();
}

/// Difference 104: shutdown closes every relay that is still registered, so the station gets its
/// BYE before the process exits.
#[tokio::test]
async fn close_all_sends_the_bye_of_every_registered_relay() {
    let r = rig().await;
    r.st.send(&invite_media("call-r6", "inv6", r.st.rtp_port()))
        .await;
    assert!(r.ev.wait("ringing|call-r6", ms(2000)).await);
    let leg = FakeLeg::new();
    join_call_to_browser_with(
        &r.ctx,
        &r.drivers,
        &r.relays,
        "127.0.0.1",
        "call-r6",
        leg.clone(),
    );
    leg.connect();
    assert!(r.st.wait_for("SIP/2.0 200", ms(3000)).await);
    let end = tokio::time::Instant::now() + ms(2000);
    while r.relays.is_empty() && tokio::time::Instant::now() < end {
        tokio::time::sleep(ms(10)).await;
    }
    assert_eq!(r.relays.len(), 1);
    r.st.collect(ms(10)).await;
    r.relays.close_all(ms(3000)).await;
    assert!(r.relays.is_empty());
    let got = r.st.collect(ms(100)).await;
    assert_eq!(
        got.iter().filter(|m| m.starts_with("BYE ")).count(),
        1,
        "{got:?}"
    );
    r.d.stop().await.unwrap();
}

/// A live relay in the hub's registry, hung up by `Hub::shutdown` before it returns (deleting the
/// `close_all` call of the shutdown makes this fail).
#[tokio::test]
async fn hub_shutdown_sends_the_bye_of_a_live_relay() {
    let st = DStation::with("ok", "200").await;
    let ep = endpoint_to(&st).await;
    let c = start_hub("shutdown", "").await;
    let relay = Arc::new(Relay::new());
    relay
        .dial_device(&ep, "portico", "9901#0", "127.0.0.1")
        .await
        .unwrap();
    c.hub.relay_registry().put("call-s1", relay.clone());
    st.collect(ms(50)).await;
    c.hub.shutdown().await;
    let got = st.collect(ms(0)).await;
    assert_eq!(
        got.iter().filter(|m| m.starts_with("BYE ")).count(),
        1,
        "{got:?}"
    );
    assert!(relay.is_closed() && c.hub.relay_registry().is_empty());
    c.srv.close().await;
}

/// The browser leg's disconnect already took the relay out of the registry and started its close;
/// `close_all` still waits for that BYE.
#[tokio::test]
async fn close_all_waits_for_a_close_a_disconnect_already_started() {
    let r = rig().await;
    r.st.send(&invite_media("call-r7", "inv7", r.st.rtp_port()))
        .await;
    assert!(r.ev.wait("ringing|call-r7", ms(2000)).await);
    let leg = FakeLeg::new();
    join_call_to_browser_with(
        &r.ctx,
        &r.drivers,
        &r.relays,
        "127.0.0.1",
        "call-r7",
        leg.clone(),
    );
    leg.connect();
    assert!(r.st.wait_for("SIP/2.0 200", ms(3000)).await);
    let end = tokio::time::Instant::now() + ms(2000);
    while r.relays.is_empty() && tokio::time::Instant::now() < end {
        tokio::time::sleep(ms(10)).await;
    }
    r.st.collect(ms(10)).await;
    r.st.set_bye_delay(400);
    leg.disconnect(); // registry emptied, close under way
    assert!(r.relays.is_empty());
    r.relays.close_all(ms(3000)).await;
    let got = r.st.collect(ms(0)).await;
    assert_eq!(
        got.iter().filter(|m| m.starts_with("BYE ")).count(),
        1,
        "close_all did not wait: {got:?}"
    );
    r.d.stop().await.unwrap();
}

/// Two relays whose stations answer the BYE after 700 ms are closed together, not one after the
/// other.
#[tokio::test]
async fn close_all_closes_relays_concurrently() {
    let (a, b) = (
        DStation::with("ok", "200").await,
        DStation::with("ok", "200").await,
    );
    a.set_bye_delay(700);
    b.set_bye_delay(700);
    let reg = RelayRegistry::new();
    for (i, st) in [&a, &b].into_iter().enumerate() {
        let ep = endpoint_to(st).await;
        let r = Arc::new(Relay::new());
        r.dial_device(&ep, "portico", "9901#0", "127.0.0.1")
            .await
            .unwrap();
        reg.put(&format!("c{i}"), r);
    }
    let t0 = std::time::Instant::now();
    reg.close_all(ms(3000)).await;
    let took = t0.elapsed();
    assert!(took < ms(1300), "closed one after the other: {took:?}");
    for st in [&a, &b] {
        assert!(st
            .st
            .lock()
            .unwrap()
            .inbox
            .iter()
            .any(|m| m.starts_with("BYE ")));
    }
}

/// Difference 105: with an empty call id the first driver in the list that has a ring gives it.
#[test]
fn find_pending_ring_without_an_id_takes_the_first_drivers_ring() {
    let (a, b) = (RingFake::new("call-a"), RingFake::new("call-b"));
    let drivers: Vec<Arc<dyn Driver>> = vec![a.clone(), b.clone()];
    assert_eq!(
        find_pending_ring(&drivers, "").unwrap().header("Call-ID"),
        "call-a"
    );
    assert_eq!(
        find_pending_ring(&drivers, "").unwrap().header("Call-ID"),
        "call-b"
    );
    assert!(find_pending_ring(&drivers, "").is_none());
}

/// Go defect (DEV-770): a ring taken by an offer cannot be CANCELled: 481, and no `ended` for it.
/// Kept as Go has it; this test pins the behaviour and names the way out (the browser leg ends).
#[tokio::test]
async fn a_cancel_after_the_offer_took_the_ring_is_481_with_no_ended_as_in_go() {
    let r = rig().await;
    r.st.send(&invite("call-r4", "inv4", true)).await;
    assert!(r.ev.wait("ringing|call-r4", ms(2000)).await);
    let leg = FakeLeg::new();
    join_call_to_browser_with(
        &r.ctx,
        &r.drivers,
        &r.relays,
        "127.0.0.1",
        "call-r4",
        leg.clone(),
    );
    r.st.collect(ms(100)).await;
    r.st.send(&cancel("call-r4", "inv4")).await;
    assert!(r.st.wait_for("SIP/2.0 481", ms(2000)).await);
    tokio::time::sleep(ms(200)).await;
    assert_eq!(r.ev.count("state|call-r4|ended"), 0);
    leg.disconnect();
    r.d.stop().await.unwrap();
}

// ---- the whole call over the IPC -------------------------------------------------------------------

struct Client {
    rd: BufReader<tokio::net::unix::OwnedReadHalf>,
    wr: tokio::net::unix::OwnedWriteHalf,
    next: i64,
    notes: Vec<Value>,
    srv: ipc::Server,
    hub: Arc<dispatch::Hub>,
    /// removed when the client is dropped (R2-158)
    _dir: common::TmpDir,
}

async fn start_hub(tag: &str, host: &str) -> Client {
    let dir = common::TmpDir::new(&format!("dahua-{}-{tag}", std::process::id()));
    let sock = dir.path().join("t.sock");
    let srv = ipc::Server::listen(&sock).unwrap();
    let hub = dispatch::register(
        &srv,
        dispatch::HubOptions {
            sfu: SessionConfig::loopback(),
            our_host: host.to_string(),
            ..dispatch::HubOptions::default()
        },
    );
    let s = srv.clone();
    tokio::spawn(async move { s.serve().await });
    let c = UnixStream::connect(&sock).await.unwrap();
    let (rd, wr) = c.into_split();
    Client {
        rd: BufReader::new(rd),
        wr,
        next: 1,
        notes: Vec::new(),
        srv,
        hub,
        _dir: dir,
    }
}

impl Client {
    async fn raw(&mut self, method: &str, params: Value) -> String {
        let id = self.next;
        self.next += 1;
        let line = format!(
            "{}\n",
            json!({"id": id, "method": method, "params": params})
        );
        self.wr.write_all(line.as_bytes()).await.unwrap();
        loop {
            let mut l = String::new();
            let n = tokio::time::timeout(Duration::from_secs(10), self.rd.read_line(&mut l))
                .await
                .expect("no response")
                .unwrap();
            assert!(n > 0, "connection closed");
            let v: Value = serde_json::from_str(&l).unwrap();
            if v.get("id").and_then(|x| x.as_i64()) == Some(id) {
                return l.trim_end().to_string();
            }
            self.notes.push(v);
        }
    }
    async fn call(&mut self, method: &str, params: Value) -> Value {
        serde_json::from_str(&self.raw(method, params).await).unwrap()
    }
    async fn note(&mut self, method: &str, within: Duration) -> Option<Value> {
        let end = tokio::time::Instant::now() + within;
        loop {
            if let Some(i) = self.notes.iter().position(|n| n["method"] == method) {
                return Some(self.notes.remove(i));
            }
            let mut l = String::new();
            match tokio::time::timeout_at(end, self.rd.read_line(&mut l)).await {
                Ok(Ok(n)) if n > 0 => self.notes.push(serde_json::from_str(&l).unwrap()),
                _ => return None,
            }
        }
    }
}

/// The tests that reach the driver through the hub share the process-wide SIP port seam.
static SIP_PORT_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

/// configure -> ring -> `webrtc_offer` of a str0m peer -> answered only once the browser is
/// connected -> audio both ways -> the browser leaves -> BYE and `ended`.
#[tokio::test]
async fn a_dahua_call_through_the_ipc_from_ring_to_hangup() {
    let _g = SIP_PORT_LOCK.lock().await;
    let st = DStation::new().await;
    let sip_port = common::dahua::SipPortGuard::set(st.port());
    let mut c = start_hub("e2e", "127.0.0.1").await;
    let r = c.call("configure", json!({"driver": "dahua_sip", "config": {"host": "127.0.0.1", "sip_user": "8001#1", "sip_password": "pw"}})).await;
    drop(sip_port);
    assert_eq!(
        r["result"].to_string(),
        r#"{"DirectCapable":true,"HasRelay":true,"InboundAudio":0,"OutboundAudio":0,"RTSPTemplate":"rtsp://{host}/cam/realmonitor?channel=1&subtype=1","ReportsAvailability":true}"#,
        "{r}"
    );
    assert!(st.wait_regs(ms(3000)).await);
    let avail = c
        .note("driver_availability", ms(2000))
        .await
        .expect("no driver_availability");
    assert_eq!(
        avail["params"],
        json!({"device_id": "127.0.0.1", "available": true, "reason": ""})
    );

    st.send(&invite_media("call-d1", "inv1", st.rtp_port()))
        .await;
    let ring = c.note("ringing", ms(2000)).await.expect("no ringing");
    assert_eq!(
        ring["params"],
        json!({"call_id": "call-d1", "device_id": "127.0.0.1"})
    );
    assert!(st.wait_for("SIP/2.0 180", ms(2000)).await);

    fn strip(t: String) -> String {
        t.split_inclusive('\n')
            .filter(|l| !l.starts_with("a=candidate:") && !l.starts_with("a=end-of-candidates"))
            .collect()
    }
    let peer = Peer::new(PeerOpts {
        opus: true,
        g722: true,
        pcmu: true,
        rewrite: Some(strip),
        ..PeerOpts::default()
    })
    .await;
    let offer = peer.offer().await;
    let r = c
        .call("webrtc_offer", json!({"call_id": "call-d1", "sdp": offer}))
        .await;
    let answer = r["result"]["sdp"]
        .as_str()
        .unwrap_or_else(|| panic!("{r}"))
        .to_string();
    assert!(
        !st.st
            .lock()
            .unwrap()
            .inbox
            .iter()
            .any(|m| m.starts_with("SIP/2.0 200")),
        "the station was answered before the browser connected"
    );
    let r = c.call("webrtc_ice", json!({"call_id": "call-d1", "candidate": format!("candidate:1 1 udp 2130706431 127.0.0.1 {} typ host", peer.addr.port())})).await;
    assert_eq!(r["result"]["ok"], true, "{r}");
    peer.accept_answer(&answer).await.unwrap();
    assert!(peer.wait_connected(10).await);
    assert!(
        st.wait_for("SIP/2.0 200", ms(3000)).await,
        "no 200 once the browser connected"
    );
    let ok = st
        .collect(ms(50))
        .await
        .into_iter()
        .find(|m| m.starts_with("SIP/2.0 200"))
        .unwrap();
    let body = ok.split_once("\r\n\r\n").unwrap().1;
    assert!(
        body.contains("c=IN IP4 127.0.0.1\r\n"),
        "the relay did not advertise -host:\n{body}"
    );
    let relay_port = parse_sdp(body.as_bytes()).unwrap().port;

    // the browser's G.722 tone reaches the station as 16 kHz L16 frames
    peer.tone(true, 1000.0, 20, 8000.0, 1.5);
    let mut heard: Vec<u8> = Vec::new();
    let end = tokio::time::Instant::now() + ms(2500);
    let mut buf = vec![0u8; 4096];
    while tokio::time::Instant::now() < end {
        if let Ok(Ok((n, _))) = tokio::time::timeout(ms(100), st.rtp.recv_from(&mut buf)).await {
            if let Ok(p) = parse_rtp_packet(&buf[..n]) {
                heard.extend(p.payload);
            }
        }
    }
    assert!(
        heard.len() > 15000,
        "the station heard {} bytes",
        heard.len()
    );
    let (a, b) = (
        common::goertzel(&heard[8000..], 16000.0, 1000.0),
        common::goertzel(&heard[8000..], 16000.0, 400.0),
    );
    assert!(a > 20.0 * b, "no 1 kHz tone at the station: {a} vs {b}");
    // and the station's frames reach the browser
    let tone = common::tone(640, 16000.0, 1000.0, 8000.0);
    for i in 0..20u16 {
        let pkt = RtpPacket {
            payload_type: 96,
            sequence_number: 100 + i,
            timestamp: 640 * i as u32,
            ssrc: 7,
            payload: [tone.clone(), tone.clone()].concat()[..RTP_FRAME_BYTES].to_vec(),
            marker: false,
        }
        .marshal();
        st.rtp
            .send_to(&pkt, format!("127.0.0.1:{relay_port}"))
            .await
            .unwrap();
        tokio::time::sleep(ms(40)).await;
    }
    assert!(
        peer.received().len() > 5,
        "the browser heard {} packets",
        peer.received().len()
    );

    // the browser leaves
    peer.close();
    assert!(
        st.wait_for("BYE ", ms(4000)).await,
        "the station was not hung up"
    );
    c.hub.shutdown().await;
    c.srv.close().await;
}

/// `driver_error` reaches Python over the IPC with Go's keys; availability goes false once.
#[tokio::test]
async fn rejected_credentials_reach_python_as_driver_error_and_availability() {
    let _g = SIP_PORT_LOCK.lock().await;
    let st = DStation::with("403", "none").await;
    let sip_port = common::dahua::SipPortGuard::set(st.port());
    let mut c = start_hub("rej", "").await;
    let r = c.call("configure", json!({"driver": "dahua_sip", "config": {"host": "127.0.0.1", "sip_user": "8001#1", "sip_password": "pw"}})).await;
    drop(sip_port);
    assert!(r.get("result").is_some(), "{r}");
    let e = c
        .note("driver_error", ms(4000))
        .await
        .expect("no driver_error");
    assert_eq!(
        e["params"],
        json!({"code": "registration_failed", "device_id": "127.0.0.1",
               "message": "sip: registration rejected (authentication failed or forbidden): 403 Forbidden"})
    );
    let a = c
        .note("driver_availability", ms(500))
        .await
        .expect("no driver_availability");
    assert_eq!(a["params"]["available"], false);
    assert!(
        c.note("driver_availability", ms(300)).await.is_none(),
        "availability reported twice"
    );
    // OpenRelay over the IPC: Go's code and text
    let r = c
        .call("open_relay", json!({"device_id": "127.0.0.1"}))
        .await;
    assert_eq!(r["error"]["code"], "open_relay_failed", "{r}");
    assert!(r["error"]["message"]
        .as_str()
        .unwrap()
        .starts_with("dahua_sip: OpenRelay not implemented — "));
    // hangup finds no driver that can end a call: Go's not_implemented
    let r = c.call("hangup", json!({"call_id": "x"})).await;
    assert_eq!(r["error"]["code"], "not_implemented", "{r}");
    // answer with no ring
    let r = c
        .call(
            "answer",
            json!({"device_id": "127.0.0.1", "host": "127.0.0.1", "port": 5000}),
        )
        .await;
    assert_eq!(r["error"]["code"], "answer_failed", "{r}");
    assert_eq!(
        r["error"]["message"],
        "dahua_sip: Answer called with no call ringing"
    );
    // a second configure of the same host is refused
    let r = c.call("configure", json!({"driver": "dahua_sip", "config": {"host": "127.0.0.1", "sip_user": "8001#1", "sip_password": "pw"}})).await;
    assert_eq!(r["error"]["code"], "already_configured", "{r}");
    // and a configure that fails validation is configure_failed with Go's text
    let r = c
        .call(
            "configure",
            json!({"driver": "dahua_sip", "config": {"host": "h2"}}),
        )
        .await;
    assert_eq!(r["error"]["code"], "configure_failed");
    assert_eq!(
        r["error"]["message"],
        "dahua_sip: host, sip_user and sip_password are required"
    );
    let r = c
        .call("deconfigure", json!({"device_id": "127.0.0.1"}))
        .await;
    assert_eq!(r["result"]["ok"], true, "{r}");
    c.hub.shutdown().await;
    c.srv.close().await;
}
