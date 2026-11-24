//! `internal/call/relay.go`: the device leg of a relay-mode call (design doc §9.3, §9.6). A
//! [`Relay`] dials the device (`dial_device`) or answers the device's own ring (`answer_device`)
//! over the shared SIP endpoint, and opens a real RTP [`AudioSession`] on a socket it binds
//! BEFORE it builds the SDP, so the port it advertises is the port it listens on. Its audio is
//! the pair of frame channels of [`DeviceLeg`].
//!
//! The client leg (the browser) is not here: the sequencer (`sequencer.rs`) and
//! `webrtc::join_call_to_browser` join the two, and never dial or answer the device before the
//! client leg has landed.
//!
//! The talkback half of Go's `Relay` (`UseTalkback`, the `TalkbackSink`) is [`Relay::use_talkback`]
//! (slice 7b): a send-only device leg whose output is a closed channel.

use crate::util::lock;
use std::net::{IpAddr, Ipv4Addr};
use std::sync::{Arc, Mutex};

use tokio::sync::Mutex as AsyncMutex;

use super::sequencer::DeviceLeg;
use crate::driver::TalkbackSink;
use crate::sip::{self, AudioSession, Call, Endpoint, IncomingRequest, SdpInfo};

/// How long `Relay::close` waits for a closed talkback to release its subprocess. Shorter than
/// the 3 s the hub gives `RelayRegistry::close_all` (R2-121): a relay with both a talkback and a
/// SIP call must still get to its BYE before the registry gives up on the close and drops it.
const TALKBACK_CLOSE_WAIT: std::time::Duration = std::time::Duration::from_secs(2);

/// `mediaHost`: the address a device-leg SDP advertises as ours. The configured one (the
/// binary's `-host`) when set, otherwise the routed one (the local address of the SIP socket that
/// talks to the device, which by construction is an address that device reaches). It must be a
/// real IPv4 address: an empty host would make the device send its RTP nowhere, silently, so an
/// empty, unspecified (`0.0.0.0`) or unparseable address is refused before any socket is opened
/// or any SIP is sent.
///
/// Differs from Go for an IPv4-mapped IPv6 literal (`::ffff:192.168.2.1`): Go's `ParseIP` accepts
/// it (`To4()` is non-nil) and writes it into `c=IN IP4 ...`, which no device can use; this
/// refuses it (PARITY.md difference 99).
pub fn media_host(configured: &str, routed: &str) -> Result<String, String> {
    let host = if configured.is_empty() {
        routed
    } else {
        configured
    };
    if host.is_empty() {
        return Err("call: no media host for the device leg's SDP: -host is not set and the SIP socket has no local address".to_string());
    }
    match host.parse::<IpAddr>() {
        Ok(IpAddr::V4(ip)) if ip != Ipv4Addr::UNSPECIFIED => Ok(host.to_string()),
        _ => Err(format!(
            "call: {} is not a usable IPv4 media host for the device leg's SDP",
            crate::gostd::strconv::go_quote(host)
        )),
    }
}

#[derive(Default)]
struct State {
    call_id: String,
    device_call: Option<Arc<Call>>,
    audio: Option<Arc<AudioSession>>,
    leg: Option<DeviceLeg>,
    /// the send-only talkback device leg (`UseTalkback`)
    talkback: Option<Arc<dyn TalkbackSink>>,
    /// `close` ran (or is running): nothing may be installed any more
    closed: bool,
}

/// The B2BUA's device leg: one instance per relay-mode call. Cheap to share (`Arc<Relay>`); every
/// method takes `&self`, no lock is held across an `.await`.
#[derive(Default)]
pub struct Relay {
    st: Mutex<State>,
    /// held for the whole of `close`, so a second `close` returns only once the first has sent
    /// its BYE (a shutdown must not overtake a close already under way)
    closing: AsyncMutex<()>,
    /// test seam: `install` waits here once the device has its answer
    #[cfg(feature = "test-seams")]
    install_gate: Mutex<Option<Arc<tokio::sync::Notify>>>,
}

impl Relay {
    pub fn new() -> Relay {
        Relay::default()
    }

    /// `&call.Relay{CallID: id}`: a relay that names its call before any device leg is up.
    pub fn with_call_id(call_id: &str) -> Relay {
        let r = Relay::default();
        r.st().call_id = call_id.to_string();
        r
    }

    /// `UseTalkback`: makes `sink` this relay's device leg: the input is the sink's, and the
    /// output is a closed channel, since a talkback path is send-only (the visitor's voice rides
    /// the RTSP feed, §9.6). A relay that was already closed closes the sink again at once
    /// (Go leaves it open until the call ends).
    pub fn use_talkback(&self, sink: Arc<dyn TalkbackSink>) {
        let (tx, rx) = tokio::sync::mpsc::channel::<Vec<u8>>(1);
        drop(tx);
        let leg = DeviceLeg {
            input: sink.input(),
            output: Arc::new(AsyncMutex::new(rx)),
        };
        let mut g = self.st();
        if g.closed {
            drop(g);
            let _ = sink.close();
            return;
        }
        g.talkback = Some(sink);
        g.leg = Some(leg);
    }

    fn st(&self) -> std::sync::MutexGuard<'_, State> {
        lock(&self.st)
    }

    /// The device dialog's Call-ID once a device leg is up (`Relay.CallID`), `""` before.
    pub fn call_id(&self) -> String {
        self.st().call_id.clone()
    }

    /// The device audio: frames to the device and from it (`AudioIn` / `AudioOut`). `None` before
    /// a device leg is up, and after `close`.
    pub fn device_leg(&self) -> Option<DeviceLeg> {
        self.st().leg.clone()
    }

    /// Test seam: the next device leg waits on `gate` after the device has its answer and before
    /// the leg is published, so a test can drop the future (or panic the task) in that window.
    #[cfg(feature = "test-seams")]
    pub fn hold_install_for_test(&self, gate: Arc<tokio::sync::Notify>) {
        *lock(&self.install_gate) = Some(gate);
    }

    /// Whether `close` has been called.
    pub fn is_closed(&self) -> bool {
        self.st().closed
    }

    /// Whether a device leg is up (and not yet closed).
    pub fn is_up(&self) -> bool {
        self.st().device_call.is_some()
    }

    /// `DialDevice`: places the device-leg SIP call and opens the real RTP audio session for it.
    /// `our_host` is the tailnet or LAN address the device can actually reach, never loopback
    /// (`""`: the address of the SIP socket that talks to the device).
    ///
    /// The RTP socket is opened BEFORE the SIP offer is built and its real bound port is what
    /// the offer advertises: a real device sends RTP to whatever port the SDP named, and
    /// advertising one port while listening on another would silently never receive audio.
    ///
    /// Real SIP/RTP traffic against a real device the moment it is called: the design doc's
    /// activation-gate rule (§7) applies exactly as it does to the driver's Dial/Answer.
    pub async fn dial_device(
        &self,
        endpoint: &Endpoint,
        our_user: &str,
        device_user: &str,
        our_host: &str,
    ) -> Result<(), String> {
        let host = media_host(our_host, &endpoint.local_host())?;
        let audio = open_audio().await?;
        let media = SdpInfo {
            host,
            port: audio.local_port() as i64,
            payload_type: 0,
        };
        let call = match sip::dial(endpoint, our_user, device_user, media, None).await {
            Ok(c) => c,
            Err(e) => {
                let _ = audio.close().await;
                return Err(format!("call: dialing device leg: {e}"));
            }
        };
        self.install(call, audio).await
    }

    /// `AnswerDevice`: the counterpart of `dial_device` for a device-originated ring (§9.3's
    /// ring-detection path): answers `req` with our own real audio socket's port (the same
    /// open-before-advertise order) instead of placing an outbound INVITE.
    pub async fn answer_device(&self, req: &IncomingRequest, our_host: &str) -> Result<(), String> {
        let host = media_host(our_host, &req.local_host())?;
        let audio = open_audio().await?;
        let media = SdpInfo {
            host,
            port: audio.local_port() as i64,
            payload_type: 0,
        };
        let call = match sip::answer_incoming(req, media).await {
            Ok(c) => c,
            Err(e) => {
                let _ = audio.close().await;
                return Err(format!("call: answering device leg: {e}"));
            }
        };
        self.install(call, audio).await
    }

    /// The tail both entry points share: point the audio at the device's negotiated address and
    /// publish the leg. A `close` that ran meanwhile (the browser left while the SIP exchange was
    /// under way) tears the new leg down at once instead of leaving it behind.
    async fn install(&self, call: Call, audio: AudioSession) -> Result<(), String> {
        // The device already has its answer (or has answered us): from here until the leg is
        // published, a future that is dropped or a task that panics must still hang the call up.
        let mut guard = ByeOnDrop(Some(Arc::new(call)));
        let call = guard.0.clone().expect("just set");
        #[cfg(feature = "test-seams")]
        {
            let gate = lock(&self.install_gate).take();
            if let Some(g) = gate {
                g.notified().await;
            }
        }
        if let Err(e) = audio.set_remote(&call.remote_sdp).await {
            let _ = audio.close().await;
            let _ = call.bye().await;
            guard.defuse();
            return Err(format!("call: targeting device-leg audio: {e}"));
        }
        let Some(out) = audio.take_audio_out() else {
            let _ = audio.close().await;
            let _ = call.bye().await;
            guard.defuse();
            return Err("call: device-leg audio has no output".to_string());
        };
        let leg = DeviceLeg {
            input: audio.audio_in(),
            output: Arc::new(AsyncMutex::new(out)),
        };
        let audio = Arc::new(audio);
        {
            let mut g = self.st();
            if !g.closed {
                g.call_id = call.call_id().to_string();
                g.device_call = Some(call);
                g.audio = Some(audio);
                g.leg = Some(leg);
                guard.defuse();
                return Ok(());
            }
        }
        let _ = audio.close().await;
        let _ = call.bye().await;
        guard.defuse();
        Err("call: the relay was closed while the device leg was coming up".to_string())
    }

    /// `Close`: ends the device leg (the audio session stops, then a BYE) and does nothing to a
    /// client leg. Idempotent (Go sends a second BYE on a second call); a second caller waits
    /// for the first to finish.
    pub async fn close(&self) -> Result<(), String> {
        let _closing = self.closing.lock().await;
        let (call, audio, talkback) = {
            let mut g = self.st();
            g.closed = true;
            g.leg = None;
            (g.device_call.take(), g.audio.take(), g.talkback.take())
        };
        if let Some(t) = talkback {
            // the session is down (the subprocess reaped) before the rest of the close goes on
            let _ = t.close();
            let _ = tokio::time::timeout(TALKBACK_CLOSE_WAIT, t.closed()).await;
        }
        if let Some(a) = audio {
            let _ = a.close().await;
        }
        if let Some(c) = call {
            return c.bye().await.map_err(|e| e.to_string());
        }
        Ok(())
    }
}

/// Sends the BYE of a device leg that was set up but never published (the future was dropped or
/// its task panicked in between), on a task of its own. Defused once the leg is published or has
/// been torn down by hand.
struct ByeOnDrop(Option<Arc<Call>>);

impl ByeOnDrop {
    fn defuse(&mut self) {
        self.0 = None;
    }
}

impl Drop for ByeOnDrop {
    fn drop(&mut self) {
        if let Some(call) = self.0.take() {
            match tokio::runtime::Handle::try_current() {
                Ok(rt) => {
                    rt.spawn(async move {
                        let _ = call.bye().await;
                    });
                }
                Err(_) => crate::panic_hook::err_line("portico-transport: warning: no runtime to hang up a device leg on; its BYE is not sent"),
            }
        }
    }
}

async fn open_audio() -> Result<AudioSession, String> {
    AudioSession::open_audio_socket(0)
        .await
        .map_err(|e| format!("call: opening device-leg audio socket: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    // TestMediaHost
    #[test]
    fn media_host_cases() {
        let cases: [(&str, &str, &str, &str, bool); 8] = [
            (
                "configured wins",
                "100.64.0.9",
                "10.0.0.2",
                "100.64.0.9",
                false,
            ),
            (
                "routed when not configured",
                "",
                "10.0.0.2",
                "10.0.0.2",
                false,
            ),
            ("nothing at all", "", "", "", true),
            ("unspecified configured", "0.0.0.0", "10.0.0.2", "", true),
            ("unspecified routed", "", "0.0.0.0", "", true),
            (
                "hostname is not an SDP address",
                "tailnet-box",
                "10.0.0.2",
                "",
                true,
            ),
            ("ipv6 cannot go in IN IP4", "fd00::1", "", "", true),
            ("garbage routed", "", "not-an-ip", "", true),
        ];
        for (name, configured, routed, want, want_err) in cases {
            let got = media_host(configured, routed);
            assert_eq!(
                got.is_err(),
                want_err,
                "{name}: media_host({configured:?},{routed:?}) = {got:?}"
            );
            assert_eq!(got.unwrap_or_default(), want, "{name}");
        }
    }

    #[test]
    fn media_host_error_texts_are_gos() {
        assert_eq!(
            media_host("", "").unwrap_err(),
            "call: no media host for the device leg's SDP: -host is not set and the SIP socket has no local address"
        );
        assert_eq!(
            media_host("tailnet-box", "").unwrap_err(),
            r#"call: "tailnet-box" is not a usable IPv4 media host for the device leg's SDP"#
        );
        assert_eq!(
            media_host("a\"b\n", "").unwrap_err(),
            r#"call: "a\"b\n" is not a usable IPv4 media host for the device leg's SDP"#
        );
    }

    // PARITY.md difference 99: every IPv4-mapped IPv6 spelling is refused (Go accepts them).
    #[test]
    fn media_host_refuses_ipv4_mapped_ipv6_that_go_accepts() {
        for h in [
            "::ffff:192.168.2.1",
            "::ffff:7f00:1",
            "0:0:0:0:0:ffff:1.2.3.4",
        ] {
            let e = media_host(h, "").unwrap_err();
            assert!(e.contains("is not a usable IPv4 media host"), "{h}: {e}");
        }
        assert!(media_host("", "::ffff:192.168.2.1").is_err());
        assert_eq!(
            media_host("192.168.2.1", "::ffff:1.1.1.1").unwrap(),
            "192.168.2.1"
        );
    }

    #[tokio::test]
    async fn a_fresh_relay_closes_as_a_no_op_and_is_marked_closed() {
        let r = Relay::new();
        assert!(!r.is_closed() && !r.is_up() && r.device_leg().is_none() && r.call_id().is_empty());
        r.close().await.unwrap();
        r.close().await.unwrap();
        assert!(r.is_closed());
    }

    // TestAnswerDevice_WithNoHostAndNoRouteFailsLoudly lives in tests/dahua_relay.rs (it needs a
    // detached request); an unusable host never opens a socket either way.
    #[tokio::test]
    async fn dial_device_with_an_unusable_host_opens_nothing() {
        let ep = Endpoint::new("127.0.0.1", 9).await.unwrap();
        let r = Relay::new();
        let e = r
            .dial_device(&ep, "portico", "9901#0", "0.0.0.0")
            .await
            .unwrap_err();
        assert!(e.contains("is not a usable IPv4 media host"), "{e}");
        assert!(r.device_leg().is_none() && r.call_id().is_empty());
        let _ = ep.close();
    }
}
