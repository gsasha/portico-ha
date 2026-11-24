//! A fake Dahua VTO on loopback for the `dahua_sip` driver and the relay (`fakeStation` of
//! `dahua_test.go`, `fakeSIPDevice` of `relay_host_test.go`): it answers REGISTER like the real
//! device (401 challenge, then 200; counts the `Expires: 0` de-registers), answers an INVITE of
//! the code under test and a BYE, records everything else, and can send requests to the code.
//! Nothing here reaches anything but 127.0.0.1.

// Each test crate that declares `mod common;` compiles every helper here and uses a subset, so an
// item unused in one crate is not dead in the suite (R1-202: a per-consumer split is the alternative).
#![allow(dead_code)]

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::net::UdpSocket;
use tokio::time::{sleep, Instant};
use transport_rs::driver::{ErrorReporter, EventSink};
use transport_rs::sip::build_sdp;

pub fn header(raw: &str, name: &str) -> String {
    let head = raw.split("\r\n\r\n").next().unwrap_or("");
    for l in head.split("\r\n") {
        if let Some((k, v)) = l.split_once(':') {
            if k.eq_ignore_ascii_case(name) {
                return v.trim().to_string();
            }
        }
    }
    String::new()
}

/// A response to `req`: Via lines, From, To (+ tag), Call-ID, CSeq echoed.
pub fn echo(
    req: &str,
    code: u16,
    reason: &str,
    to_tag: &str,
    body: &str,
    extra: &[&str],
) -> String {
    let head = req.split("\r\n\r\n").next().unwrap_or("");
    let mut out = vec![format!("SIP/2.0 {code} {reason}")];
    for l in head.split("\r\n") {
        if let Some((k, _)) = l.split_once(':') {
            if k.eq_ignore_ascii_case("Via") {
                out.push(l.to_string());
            }
        }
    }
    out.push(format!("From: {}", header(req, "From")));
    let mut to = header(req, "To");
    if !to_tag.is_empty() && !to.contains("tag=") {
        to.push_str(&format!(";tag={to_tag}"));
    }
    out.push(format!("To: {to}"));
    out.push(format!("Call-ID: {}", header(req, "Call-ID")));
    out.push(format!("CSeq: {}", header(req, "CSeq")));
    out.extend(extra.iter().map(|x| x.to_string()));
    if !body.is_empty() {
        out.push("Content-Type: application/sdp".to_string());
    }
    out.push(format!("Content-Length: {}", body.len()));
    format!("{}\r\n\r\n{body}", out.join("\r\n"))
}

#[derive(Default)]
pub struct DState {
    pub inbox: Vec<String>,
    pub drv: Option<SocketAddr>,
    pub regs: u32,
    pub deregs: u32,
    /// ok | 403 | 486 | silent
    pub reg_mode: String,
    /// 200 | 486 | none
    pub inv_mode: String,
    /// the media port in the last SDP the code put on the wire
    pub media_port: u16,
    /// a BYE of the code under test is recorded and never answered
    pub bye_silent: bool,
    /// a BYE of the code under test is answered after this long (ms)
    pub bye_delay_ms: u64,
}

pub struct DStation {
    pub sip: Arc<UdpSocket>,
    pub rtp: Arc<UdpSocket>,
    pub st: Arc<Mutex<DState>>,
    task: tokio::task::JoinHandle<()>,
}

impl DStation {
    pub async fn new() -> DStation {
        DStation::with("ok", "none").await
    }

    pub async fn with(reg_mode: &str, inv_mode: &str) -> DStation {
        let sip = Arc::new(UdpSocket::bind("127.0.0.1:0").await.unwrap());
        let rtp = Arc::new(UdpSocket::bind("127.0.0.1:0").await.unwrap());
        let st = Arc::new(Mutex::new(DState {
            reg_mode: reg_mode.into(),
            inv_mode: inv_mode.into(),
            ..Default::default()
        }));
        let (s2, r2, st2) = (sip.clone(), rtp.clone(), st.clone());
        let task = tokio::spawn(async move {
            let mut buf = vec![0u8; 8192];
            loop {
                let Ok((n, from)) = s2.recv_from(&mut buf).await else {
                    return;
                };
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                let (reg_mode, inv_mode) = {
                    let mut g = st2.lock().unwrap();
                    if g.drv.is_none() {
                        g.drv = Some(from);
                    }
                    (g.reg_mode.clone(), g.inv_mode.clone())
                };
                let reply = |x: String| {
                    let s3 = s2.clone();
                    async move {
                        let _ = s3.send_to(x.as_bytes(), from).await;
                    }
                };
                if req.starts_with("REGISTER ") {
                    match reg_mode.as_str() {
                        "403" => reply(echo(&req, 403, "Forbidden", "", "", &[])).await,
                        "486" => reply(echo(&req, 486, "Busy Here", "", "", &[])).await,
                        "silent" => {}
                        _ if !req.contains("Authorization:") => reply(echo(
                            &req,
                            401,
                            "Unauthorized",
                            "",
                            "",
                            &[r#"WWW-Authenticate: Digest realm="r", nonce="n1", algorithm=MD5"#],
                        ))
                        .await,
                        _ if req.contains("Expires: 0") => {
                            st2.lock().unwrap().deregs += 1;
                            reply(echo(&req, 200, "OK", "", "", &["Expires: 0"])).await
                        }
                        _ => {
                            st2.lock().unwrap().regs += 1;
                            // "expires1": grants one second, so the registrar refreshes at once
                            let granted = if reg_mode == "expires1" {
                                "Expires: 1"
                            } else {
                                "Expires: 3600"
                            };
                            reply(echo(&req, 200, "OK", "", "", &[granted])).await
                        }
                    }
                    continue;
                }
                if req.starts_with("INVITE ") {
                    match inv_mode.as_str() {
                        "200" => {
                            let ra = r2.local_addr().unwrap();
                            let sdp = build_sdp(&ra.ip().to_string(), ra.port() as i64);
                            reply(echo(&req, 200, "OK", "devicetag", &sdp, &[])).await
                        }
                        "486" => reply(echo(&req, 486, "Busy Here", "devicetag", "", &[])).await,
                        _ => {}
                    }
                } else if req.starts_with("BYE ") && !st2.lock().unwrap().bye_silent {
                    let d = st2.lock().unwrap().bye_delay_ms;
                    if d > 0 {
                        sleep(Duration::from_millis(d)).await;
                    }
                    reply(echo(&req, 200, "OK", "", "", &[])).await;
                }
                let mut g = st2.lock().unwrap();
                if req.starts_with("INVITE ") || req.starts_with("SIP/2.0 200") {
                    if let Some(i) = req.find("m=audio ") {
                        let rest = &req[i + "m=audio ".len()..];
                        if let Some(j) = rest.find(' ') {
                            if let Ok(p) = rest[..j].parse::<u16>() {
                                g.media_port = p;
                            }
                        }
                    }
                }
                g.inbox.push(req);
            }
        });
        DStation { sip, rtp, st, task }
    }

    pub fn port(&self) -> u16 {
        self.sip.local_addr().unwrap().port()
    }

    pub fn rtp_port(&self) -> u16 {
        self.rtp.local_addr().unwrap().port()
    }

    pub fn set_inv_mode(&self, m: &str) {
        self.st.lock().unwrap().inv_mode = m.to_string();
    }

    pub fn set_bye_silent(&self, on: bool) {
        self.st.lock().unwrap().bye_silent = on;
    }

    pub fn set_bye_delay(&self, ms: u64) {
        self.st.lock().unwrap().bye_delay_ms = ms;
    }

    pub fn counts(&self) -> (u32, u32) {
        let g = self.st.lock().unwrap();
        (g.regs, g.deregs)
    }

    pub async fn wait_regs(&self, within: Duration) -> bool {
        let end = Instant::now() + within;
        while Instant::now() < end {
            if self.counts().0 > 0 {
                return true;
            }
            sleep(Duration::from_millis(10)).await;
        }
        false
    }

    /// What arrived since the last call (after `window`), in order.
    pub async fn collect(&self, window: Duration) -> Vec<String> {
        sleep(window).await;
        std::mem::take(&mut self.st.lock().unwrap().inbox)
    }

    /// Waits until a datagram starting with `prefix` has arrived (it stays in the inbox).
    pub async fn wait_for(&self, prefix: &str, within: Duration) -> bool {
        let end = Instant::now() + within;
        while Instant::now() < end {
            if self
                .st
                .lock()
                .unwrap()
                .inbox
                .iter()
                .any(|m| m.starts_with(prefix))
            {
                return true;
            }
            sleep(Duration::from_millis(10)).await;
        }
        false
    }

    /// Sends `raw` to the code under test (the source of its first datagram).
    pub async fn send(&self, raw: &str) {
        let to = self
            .st
            .lock()
            .unwrap()
            .drv
            .expect("the code has not spoken yet");
        self.sip.send_to(raw.as_bytes(), to).await.unwrap();
    }

    pub fn media_port(&self) -> u16 {
        self.st.lock().unwrap().media_port
    }
}

impl Drop for DStation {
    fn drop(&mut self) {
        self.task.abort();
    }
}

pub const SDP4000: &str =
    "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 4000 RTP/AVP 0\r\n";

/// The station's INVITE for `id` (Via branch `z9hG4bK<br>`), with an SDP body when `sdp`.
pub fn invite(id: &str, br: &str, sdp: bool) -> String {
    let h = format!(
        "INVITE sip:8001@127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1;branch=z9hG4bK{br}\r\n\
         From: <sip:vto@127.0.0.1>;tag=a\r\nTo: <sip:8001@127.0.0.1>\r\nCall-ID: {id}\r\nCSeq: 1 INVITE\r\n"
    );
    if sdp {
        format!(
            "{h}Content-Type: application/sdp\r\nContent-Length: {}\r\n\r\n{SDP4000}",
            SDP4000.len()
        )
    } else {
        format!("{h}Content-Length: 0\r\n\r\n")
    }
}

/// The station's INVITE whose SDP names the station's real RTP socket (`rtp_port`), so the audio
/// the code sends reaches the test.
pub fn invite_media(id: &str, br: &str, rtp_port: u16) -> String {
    let sdp = build_sdp("127.0.0.1", rtp_port as i64);
    format!(
        "INVITE sip:8001@127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1;branch=z9hG4bK{br}\r\n\
         From: <sip:vto@127.0.0.1>;tag=a\r\nTo: <sip:8001@127.0.0.1>\r\nCall-ID: {id}\r\nCSeq: 1 INVITE\r\n\
         Content-Type: application/sdp\r\nContent-Length: {}\r\n\r\n{sdp}",
        sdp.len()
    )
}

pub fn cancel(id: &str, br: &str) -> String {
    format!(
        "CANCEL sip:8001@127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1;branch=z9hG4bK{br}\r\n\
         From: <sip:vto@127.0.0.1>;tag=a\r\nTo: <sip:8001@127.0.0.1>\r\nCall-ID: {id}\r\nCSeq: 1 CANCEL\r\nContent-Length: 0\r\n\r\n"
    )
}

pub fn bye(id: &str, br: &str) -> String {
    format!(
        "BYE sip:8001@127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1;branch=z9hG4bK{br}\r\n\
         From: <sip:vto@127.0.0.1>;tag=a\r\nTo: <sip:8001@127.0.0.1>;tag=b\r\nCall-ID: {id}\r\nCSeq: 2 BYE\r\nContent-Length: 0\r\n\r\n"
    )
}

pub fn first_line(m: &str) -> &str {
    m.split("\r\n").next().unwrap_or("")
}

pub fn to_tag_of(m: &str) -> String {
    let to = header(m, "To");
    match to.find("tag=") {
        Some(i) => to[i + 4..].to_string(),
        None => String::new(),
    }
}

/// A recording sink with the `errorReporter` extension (`regEvents` / `countEvents` of the Go
/// tests): events as strings, in order.
#[derive(Default)]
pub struct Rec {
    ev: Mutex<Vec<String>>,
}

impl Rec {
    pub fn new() -> Arc<Rec> {
        Arc::new(Rec::default())
    }
    pub fn events(&self) -> Vec<String> {
        self.ev.lock().unwrap().clone()
    }
    pub fn count(&self, prefix: &str) -> usize {
        self.events()
            .iter()
            .filter(|e| e.starts_with(prefix))
            .count()
    }
    pub async fn wait(&self, prefix: &str, within: Duration) -> bool {
        let end = Instant::now() + within;
        while Instant::now() < end {
            if self.count(prefix) > 0 {
                return true;
            }
            sleep(Duration::from_millis(10)).await;
        }
        false
    }
}

impl EventSink for Rec {
    fn ringing(&self, call_id: &str, device_id: &str) {
        self.ev
            .lock()
            .unwrap()
            .push(format!("ringing|{call_id}|{device_id}"));
    }
    fn state_changed(&self, call_id: &str, state: &str) {
        self.ev
            .lock()
            .unwrap()
            .push(format!("state|{call_id}|{state}"));
    }
    fn availability_changed(&self, id: &str, ok: bool, reason: &str) {
        self.ev
            .lock()
            .unwrap()
            .push(format!("avail|{id}|{ok}|{reason}"));
    }
    fn error_reporter(&self) -> Option<&dyn ErrorReporter> {
        Some(self)
    }
}

impl ErrorReporter for Rec {
    fn driver_error(&self, id: &str, code: &str, msg: &str) {
        self.ev
            .lock()
            .unwrap()
            .push(format!("error|{id}|{code}|{msg}"));
    }
}

/// Points the dahua driver at a fake station for the lifetime of the guard (the process-wide
/// `SIP_PORT`), and resets it when the guard is dropped, a failed assertion included: a port left
/// set would make every later test of the binary fail in a misleading way.
pub struct SipPortGuard;

impl SipPortGuard {
    pub fn set(port: u16) -> SipPortGuard {
        transport_rs::driver::dahua::SIP_PORT
            .store(port as i64, std::sync::atomic::Ordering::SeqCst);
        SipPortGuard
    }
}

impl Drop for SipPortGuard {
    fn drop(&mut self) {
        transport_rs::driver::dahua::SIP_PORT.store(0, std::sync::atomic::Ordering::SeqCst);
    }
}
