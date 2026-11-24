//! A fake door station on loopback and a harness around a real `gate_sip_server` driver
//! (`helpers_test.go`). Nothing here reaches anything but 127.0.0.0/8: the station and the server
//! both bind loopback, and the one wildcard-bind test only ever talks to 127.0.0.1.

// Each test crate that declares `mod common;` compiles every helper here and uses a subset, so an
// item unused in one crate is not dead in the suite (R1-202: a per-consumer split is the alternative).
#![allow(dead_code)]

pub mod dahua;
pub mod golden;
pub mod house;
pub mod peer;
pub mod talk;
pub mod unifi;

use std::net::{IpAddr, SocketAddr};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use md5::{Digest, Md5};
use serde_json::{json, Map, Value};
use tokio::net::UdpSocket;
use tokio::time::Instant;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::gatesipserver::{AudioClient, GateSipServer, Server};
use transport_rs::driver::{CapabilitiesSink, Driver, EventSink};

/// A temporary directory removed when it is dropped.
pub struct TmpDir(std::path::PathBuf);

impl TmpDir {
    pub fn new(name: &str) -> TmpDir {
        let p = std::env::temp_dir().join(name);
        std::fs::create_dir_all(&p).unwrap();
        TmpDir(p)
    }
    pub fn path(&self) -> &std::path::Path {
        &self.0
    }
}

impl Drop for TmpDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// `sink`: records the events a driver reports, as `ringing:<id>@<device>`, `<state>:<id>` and
/// `availability`; capability changes as `caps:<device>:<CanDial>` (the Go helper's sink does
/// not implement `CapabilitiesSink`; this one does so the republish can be asserted).
#[derive(Default)]
pub struct Sink {
    ev: Mutex<Vec<String>>,
}

impl Sink {
    fn add(&self, x: String) {
        self.ev.lock().unwrap().push(x);
    }
    pub fn events(&self) -> Vec<String> {
        self.ev.lock().unwrap().clone()
    }
    /// Polls until the recorded events equal `want`, or fails.
    pub async fn wait_events(&self, want: &[&str]) {
        if want.is_empty() {
            // "nothing was reported" is true on the first poll: settle first (R2-93)
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        let end = Instant::now() + Duration::from_secs(3);
        let mut got = Vec::new();
        while Instant::now() < end {
            got = self.events();
            if got == want {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("events:\n got {got:?}\nwant {want:?}");
    }
}

impl EventSink for Sink {
    fn ringing(&self, call_id: &str, device_id: &str) {
        self.add(format!("ringing:{call_id}@{device_id}"));
    }
    fn state_changed(&self, call_id: &str, state: &str) {
        self.add(format!("{state}:{call_id}"));
    }
    fn availability_changed(&self, _: &str, _: bool, _: &str) {
        self.add("availability".into());
    }
    fn capabilities_sink(&self) -> Option<&dyn CapabilitiesSink> {
        Some(self)
    }
}

impl CapabilitiesSink for Sink {
    fn capabilities_changed(&self, device_id: &str, caps: &transport_rs::driver::Capabilities) {
        self.add(format!("caps:{device_id}:{}", caps.can_dial));
    }
}

/// `msg`: one SIP datagram the station received.
#[derive(Clone, Debug)]
pub struct Msg {
    pub code: u16,
    pub method: String,
    pub raw: String,
    /// the datagram exactly as received
    pub bytes: Vec<u8>,
}

impl Msg {
    pub fn body(&self) -> &str {
        self.raw.split_once("\r\n\r\n").map_or("", |(_, b)| b)
    }

    /// `(?im)^name:\s*(.*)$`, trimmed; "" when absent.
    pub fn header(&self, name: &str) -> String {
        let head = self.raw.split("\r\n\r\n").next().unwrap_or("");
        for l in head.lines() {
            if let Some((k, v)) = l.split_once(':') {
                if k.eq_ignore_ascii_case(name) {
                    return v.trim().to_string();
                }
            }
        }
        String::new()
    }
}

pub fn codes(ms: &[Msg]) -> Vec<String> {
    ms.iter()
        .map(|m| {
            if m.code != 0 {
                m.code.to_string()
            } else {
                m.method.clone()
            }
        })
        .collect()
}

pub fn has(ms: &[Msg], what: &str) -> bool {
    codes(ms).iter().any(|c| c == what)
}

pub fn find(ms: &[Msg], what: &str) -> Option<Msg> {
    ms.iter()
        .find(|m| m.method == what || (m.code != 0 && m.code.to_string() == what))
        .cloned()
}

pub fn md5hex(s: &str) -> String {
    md5hex_bytes(s.as_bytes())
}

pub fn md5hex_bytes(b: &[u8]) -> String {
    Md5::digest(b).iter().map(|b| format!("{b:02x}")).collect()
}

/// `digestAuth`: answers a challenge the way the station does (RFC 2617, no qop).
pub fn digest_auth(challenge: &Msg, user: &str, pass: &str) -> String {
    let h = challenge.header("WWW-Authenticate");
    let realm = quoted(&h, "realm");
    let nonce = quoted(&h, "nonce");
    digest_for(user, pass, &realm, &nonce)
}

pub fn digest_for(user: &str, pass: &str, realm: &str, nonce: &str) -> String {
    let uri = "sip:127.0.0.1";
    let resp = md5hex(&format!(
        "{}:{nonce}:{}",
        md5hex(&format!("{user}:{realm}:{pass}")),
        md5hex(&format!("REGISTER:{uri}"))
    ));
    format!(
        "Digest username=\"{user}\", realm=\"{realm}\", nonce=\"{nonce}\", uri=\"{uri}\", response=\"{resp}\", algorithm=MD5"
    )
}

/// The value of `key="..."` in a header.
pub fn quoted(h: &str, key: &str) -> String {
    let pat = format!("{key}=\"");
    let Some(i) = h.find(&pat) else {
        return String::new();
    };
    let rest = &h[i + pat.len()..];
    rest[..rest.find('"').unwrap_or(rest.len())].to_string()
}

/// `station`.
pub struct Station {
    pub conn: UdpSocket,
    pub srv: SocketAddr,
    /// the media socket the SDP names
    pub rtp: UdpSocket,
    pub me: SocketAddr,
}

impl Station {
    pub async fn new(srv: SocketAddr) -> Station {
        Station::on(srv, "127.0.0.1").await
    }

    pub async fn on(srv: SocketAddr, ip: &str) -> Station {
        let ip: IpAddr = ip.parse().unwrap();
        let conn = UdpSocket::bind((ip, 0)).await.unwrap();
        let rtp = UdpSocket::bind((ip, 0)).await.unwrap();
        let me = conn.local_addr().unwrap();
        Station {
            conn,
            srv: SocketAddr::new("127.0.0.1".parse().unwrap(), srv.port()),
            rtp,
            me,
        }
    }

    pub fn rtp_port(&self) -> u16 {
        self.rtp.local_addr().unwrap().port()
    }

    pub fn sdp(&self, kind: &str) -> String {
        let head = format!(
            "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=x\r\nc=IN IP4 {}\r\nt=0 0\r\n",
            self.me.ip()
        );
        let p = self.rtp_port();
        match kind {
            "narrow" => format!("{head}m=audio {p} RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\n"),
            "wide" => format!(
                "{head}m=audio {p} RTP/AVP 96 8\r\na=rtpmap:96 PCM/16000\r\na=rtpmap:8 PCMA/8000\r\n"
            ),
            // the station's real menu (media.go header)
            "menu" => format!(
                "{head}m=audio {p} RTP/AVP 9 96 97 8 0\r\na=rtpmap:9 G722/8000\r\na=rtpmap:96 PCM/16000\r\na=rtpmap:97 PCM/8000\r\na=rtpmap:8 PCMA/8000\r\na=rtpmap:0 PCMU/8000\r\n"
            ),
            "g722only" => format!("{head}m=audio {p} RTP/AVP 9\r\na=rtpmap:9 G722/16000\r\n"),
            "l16" => format!("{head}m=audio {p} RTP/AVP 100\r\na=rtpmap:100 L16/16000\r\n"),
            "pcmuonly" => format!("{head}m=audio {p} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"),
            other => panic!("unknown sdp kind {other:?}"),
        }
    }

    pub async fn send(&self, s: &str) {
        self.conn.send_to(s.as_bytes(), self.srv).await.unwrap();
    }

    pub async fn invite(&self, call_id: &str, cseq: u32, sdp: &str) {
        self.send(&self.invite_text(call_id, cseq, sdp)).await;
    }

    pub fn invite_text(&self, call_id: &str, cseq: u32, sdp: &str) -> String {
        format!(
            "INVITE sip:9901@127.0.0.1:{port} SIP/2.0\r\n\
             Via: SIP/2.0/UDP {me};branch=z9hG4bK{call_id}\r\n\
             From: <sip:8001@station>;tag=t-{call_id}\r\n\
             To: <sip:9901@server>\r\n\
             Call-ID: {call_id}\r\nCSeq: {cseq} INVITE\r\n\
             Contact: <sip:8001@{me}>\r\n\
             Content-Type: application/sdp\r\nContent-Length: {len}\r\n\r\n{sdp}",
            port = self.srv.port(),
            me = self.me,
            len = sdp.len(),
        )
    }

    pub async fn simple(&self, method: &str, call_id: &str, cseq: u32) {
        self.send(&self.simple_text(method, call_id, cseq)).await;
    }

    pub fn simple_text(&self, method: &str, call_id: &str, cseq: u32) -> String {
        format!(
            "{method} sip:9901@127.0.0.1:{port} SIP/2.0\r\n\
             Via: SIP/2.0/UDP {me};branch=z9hG4bK{call_id}-{lower}\r\n\
             From: <sip:8001@station>;tag=t-{call_id}\r\n\
             To: <sip:9901@server>\r\nCall-ID: {call_id}\r\nCSeq: {cseq} {method}\r\nContent-Length: 0\r\n\r\n",
            port = self.srv.port(),
            me = self.me,
            lower = method.to_lowercase(),
        )
    }

    pub async fn bye(&self, call_id: &str) {
        self.simple("BYE", call_id, 2).await;
    }
    pub async fn cancel(&self, call_id: &str) {
        self.simple("CANCEL", call_id, 1).await;
    }
    pub async fn ack(&self, call_id: &str) {
        self.simple("ACK", call_id, 1).await;
    }
    pub async fn options(&self) {
        self.simple("OPTIONS", "opt", 1).await;
    }

    pub async fn register(&self, auth: &str) {
        self.send(&self.register_text(auth)).await;
    }

    /// A REGISTER with its own `Expires` header and Contact parameters (de-registration).
    pub async fn register_expiring(&self, auth: &str, expires: &str, contact_params: &str) {
        let t = self
            .register_text(auth)
            .replace("Expires: 300", &format!("Expires: {expires}"))
            .replace(
                &format!("Contact: <sip:8001@{}>", self.me),
                &format!("Contact: <sip:8001@{}>{contact_params}", self.me),
            );
        self.send(&t).await;
    }

    pub async fn send_bytes(&self, b: &[u8]) {
        self.conn.send_to(b, self.srv).await.unwrap();
    }

    pub fn register_text(&self, auth: &str) -> String {
        let a = if auth.is_empty() {
            String::new()
        } else {
            format!("Authorization: {auth}\r\n")
        };
        format!(
            "REGISTER sip:127.0.0.1:{port} SIP/2.0\r\n\
             Via: SIP/2.0/UDP {me};branch=z9hG4bKreg\r\nFrom: <sip:8001@station>;tag=reg\r\nTo: <sip:8001@station>\r\n\
             Call-ID: reg-1\r\nCSeq: 1 REGISTER\r\nContact: <sip:8001@{me}>\r\nExpires: 300\r\n{a}Content-Length: 0\r\n\r\n",
            port = self.srv.port(),
            me = self.me,
        )
    }

    /// `collect`: every SIP message that arrives within the window.
    pub async fn collect(&self, window: Duration) -> Vec<Msg> {
        let mut out = Vec::new();
        let mut buf = vec![0u8; 65535];
        let end = Instant::now() + window;
        while let Ok(Ok((n, _))) = tokio::time::timeout_at(end, self.conn.recv_from(&mut buf)).await
        {
            let raw = String::from_utf8_lossy(&buf[..n]).into_owned();
            let mut m = Msg {
                code: 0,
                method: String::new(),
                raw,
                bytes: buf[..n].to_vec(),
            };
            if let Some(rest) = m.raw.strip_prefix("SIP/2.0 ") {
                m.code = rest.get(..3).and_then(|c| c.parse().ok()).unwrap_or(0);
            } else if let Some((meth, _)) = m.raw.split_once(" sip:") {
                m.method = meth.to_string();
            }
            out.push(m);
        }
        out
    }

    /// Reads one RTP datagram from the server (the media socket), header and payload.
    pub async fn read_rtp(&self, within: Duration) -> Option<(Vec<u8>, Vec<u8>)> {
        let mut buf = vec![0u8; 4096];
        match tokio::time::timeout(within, self.rtp.recv_from(&mut buf)).await {
            Ok(Ok((n, _))) if n >= 12 => Some((buf[..12].to_vec(), buf[12..n].to_vec())),
            _ => None,
        }
    }
}

/// `registerStation`: the full digest REGISTER.
pub async fn register_station(h: &Harness) {
    h.st.register("").await;
    let m401 = find(&h.st.collect(Duration::from_millis(300)).await, "401").expect("no challenge");
    h.st.register(&digest_auth(&m401, "8001", "s3cretpass"))
        .await;
    assert!(
        find(&h.st.collect(Duration::from_millis(300)).await, "200").is_some(),
        "register refused"
    );
}

/// `harness`.
pub struct Harness {
    pub d: Arc<GateSipServer>,
    pub s: Arc<Server>,
    pub sink: Arc<Sink>,
    pub st: Station,
    pub ctx: CancelToken,
}

#[derive(Default)]
pub struct Opts {
    pub cfg: Map<String, Value>,
    /// (ring limit, orphan grace) replacing the defaults of 700 ms / 600 ms
    pub ring: Option<Duration>,
    pub orphan: Option<Duration>,
    /// DEV-796 S1: keep the real rule (an INVITE only from the registered station). By default
    /// the harness opens the server to any allowed host, as the Go-ported tests expect.
    pub strict_invites: bool,
}

impl Opts {
    pub fn ring(ms: u64) -> Opts {
        Opts {
            ring: Some(Duration::from_millis(ms)),
            ..Opts::default()
        }
    }
    pub fn orphan(ms: u64) -> Opts {
        Opts {
            orphan: Some(Duration::from_millis(ms)),
            ..Opts::default()
        }
    }
    pub fn cfg(v: Value) -> Opts {
        Opts {
            cfg: v.as_object().cloned().unwrap_or_default(),
            ..Opts::default()
        }
    }
}

impl Harness {
    pub async fn new() -> Harness {
        Harness::with(Opts::default()).await
    }

    pub async fn with(o: Opts) -> Harness {
        let mut cfg = json!({"sip_listen": "127.0.0.1:0", "password": "s3cretpass", "rtp_port": 0})
            .as_object()
            .cloned()
            .unwrap();
        for (k, v) in o.cfg {
            cfg.insert(k, v);
        }
        let mut d = GateSipServer::default();
        d.configure(&cfg).unwrap();
        // Short timers: gate-sip's 45 s / 15 s defaults are untestable in a test that has to
        // finish. Configure takes whole seconds; tests set fractions here.
        d.set_timers(
            o.ring.unwrap_or(Duration::from_millis(700)),
            o.orphan.unwrap_or(Duration::from_millis(600)),
        );
        let sink = Arc::new(Sink::default());
        let ctx = CancelToken::new();
        d.start(&ctx, sink.clone()).await.unwrap();
        let s = d.real_server().unwrap();
        s.set_open_invites(!o.strict_invites);
        let st = Station::new(s.sip_addr()).await;
        Harness {
            d: Arc::new(d),
            s,
            sink,
            st,
            ctx,
        }
    }

    pub fn device_id(&self) -> String {
        self.s.device_id().to_string()
    }

    /// The address the station sends RTP to: the port the server's SDP named.
    pub fn rtp_addr(&self) -> SocketAddr {
        SocketAddr::new("127.0.0.1".parse().unwrap(), self.s.rtp_port())
    }
}

impl Drop for Harness {
    fn drop(&mut self) {
        // `t.Cleanup(func() { d.Stop() })`: the tests that need the stop awaited call it
        // themselves; this releases the tasks of the rest.
        self.ctx.cancel();
        if let Some(s) = self.d.real_server() {
            let _ = tokio::runtime::Handle::try_current().map(|rt| {
                rt.spawn(async move { s.shutdown().await });
            });
        }
    }
}

/// `frameWithin`: waits for one frame (or the close of the channel). `(frame, open, got)`.
pub async fn frame_within(
    rx: &mut tokio::sync::mpsc::Receiver<Vec<u8>>,
    d: Duration,
) -> (Option<Vec<u8>>, bool, bool) {
    match tokio::time::timeout(d, rx.recv()).await {
        Ok(Some(f)) => (Some(f), true, true),
        Ok(None) => (None, false, true),
        Err(_) => (None, true, false),
    }
}

/// `closedWithin`: the client's frames channel closes within `d` (frames are discarded).
pub async fn closed_within(rx: &mut tokio::sync::mpsc::Receiver<Vec<u8>>, d: Duration) -> bool {
    let end = Instant::now() + d;
    loop {
        match tokio::time::timeout_at(end, rx.recv()).await {
            Ok(Some(_)) => {}
            Ok(None) => return true,
            Err(_) => return false,
        }
    }
}

/// Takes the client's frames, as the downlink does (once).
pub fn frames_of(cl: &Arc<AudioClient>) -> tokio::sync::mpsc::Receiver<Vec<u8>> {
    cl.take_frames().expect("frames already taken")
}

/// `goertzel`: the power at `freq` in 16-bit LE PCM at `rate`.
pub fn goertzel(pcm: &[u8], rate: f64, freq: f64) -> f64 {
    let w = 2.0 * std::f64::consts::PI * freq / rate;
    let coeff = 2.0 * w.cos();
    let (mut s1, mut s2) = (0.0f64, 0.0f64);
    let mut i = 0;
    while i + 1 < pcm.len() {
        let x = f64::from(i16::from_le_bytes([pcm[i], pcm[i + 1]]));
        let s0 = x + coeff * s1 - s2;
        s2 = s1;
        s1 = s0;
        i += 2;
    }
    s1 * s1 + s2 * s2 - coeff * s1 * s2
}

pub fn rtp_packet(pt: u8, seq: u16, ts: u32, payload: &[u8]) -> Vec<u8> {
    let mut h = vec![0x80, pt];
    h.extend_from_slice(&seq.to_be_bytes());
    h.extend_from_slice(&ts.to_be_bytes());
    h.extend_from_slice(&[0xDE, 0xAD, 0xBE, 0xEF]);
    h.extend_from_slice(payload);
    h
}

/// `tone(samples, rate, freq, amp)`.
pub fn tone(samples: usize, rate: f64, freq: f64, amp: f64) -> Vec<u8> {
    let mut b = Vec::with_capacity(samples * 2);
    for i in 0..samples {
        let v = (amp * (2.0 * std::f64::consts::PI * freq * i as f64 / rate).sin()) as i16;
        b.extend_from_slice(&v.to_le_bytes());
    }
    b
}

pub async fn sleep_ms(ms: u64) {
    tokio::time::sleep(Duration::from_millis(ms)).await;
}

pub fn ms(n: u64) -> Duration {
    Duration::from_millis(n)
}
