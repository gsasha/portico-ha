//! `pkg/driver/driver.go`: the Driver interface family (design doc §4.2), the capability set
//! the `configure` reply carries, and the error values that become IPC error codes.
//!
//! Go's optional interfaces (`AudioClaimer`, `CallHangupper`, `ConnectAnswerer`, ...) are found
//! by type assertion; here each is a trait and [`Driver`] has an `as_*` accessor per trait that
//! returns `None` unless the driver implements it (the accessor IS the type assertion). A Go
//! `context.Context` argument is a [`CancelToken`]; a method that blocks is an `async` method
//! returning a [`BoxFuture`] so the traits stay object-safe without a dependency.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use serde_json::{Map, Value};
use tokio::sync::mpsc;

use crate::cancel::CancelToken;
use crate::gojson;
use crate::ipc::IpcError;
use crate::sip::IncomingRequest;

pub mod dahua;
pub mod gatesipserver;
pub mod unifiprotect;

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// How a driver's audio actually moves (`AudioTopology`, an `int` on the wire).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AudioTopology(pub i64);

impl AudioTopology {
    pub const SIP_RTP: AudioTopology = AudioTopology(0);
    pub const IN_VIDEO_STREAM: AudioTopology = AudioTopology(1);
    pub const TALKBACK_PUSH: AudioTopology = AudioTopology(2);
    pub const NO_AUDIO: AudioTopology = AudioTopology(3);
}

/// Where a driver should send/expect RTP for a call (`MediaTarget`).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct MediaTarget {
    pub host: String,
    pub port: i64,
}

/// The single source of truth for what a driver can do (`Capabilities`). Python never carries
/// its own copy; the `configure` reply is this struct.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Capabilities {
    pub inbound_audio: AudioTopology,
    pub outbound_audio: AudioTopology,
    pub direct_capable: bool,
    pub has_relay: bool,
    pub rtsp_template: String,
    /// True for a driver that reports its device's reachability through
    /// [`EventSink::availability_changed`].
    pub reports_availability: bool,
    /// True for a driver that can place a call and follow it (it implements [`CallDialer`])
    /// AND is, right now, configured and permitted to. Omitted from the wire when false so a
    /// driver that does not set it keeps a configure reply byte-identical to what it was before
    /// the field existed.
    pub can_dial: bool,
    /// DEV-518: only `gate_sip_server` sets it. `Some(b)` = the driver reports whether the door
    /// station holds a live registration, independent of `dial_enabled`; `None` (every other
    /// driver, and any older wheel) keeps the key off the wire.
    pub station_registered: Option<bool>,
    /// DEV-518: unix seconds of the last good REGISTER (0 = none yet; omitted from the wire).
    pub last_register_at: u64,
    /// DEV-811 P3: what this wheel can do beyond the capability flags above (`call_stats`). A list,
    /// not a version number, because a revert wheel is older source under a higher version. Omitted
    /// from the wire when empty (every driver but `gate_sip_server`, and any older wheel).
    pub features: Vec<&'static str>,
}

impl Default for Capabilities {
    fn default() -> Self {
        Capabilities {
            inbound_audio: AudioTopology::SIP_RTP,
            outbound_audio: AudioTopology::SIP_RTP,
            direct_capable: false,
            has_relay: false,
            rtsp_template: String::new(),
            reports_availability: false,
            can_dial: false,
            station_registered: None,
            last_register_at: 0,
            features: Vec::new(),
        }
    }
}

impl Capabilities {
    /// `json.Marshal(caps)`: the Go struct has no JSON tags, so the wire carries the Go field
    /// names, in declaration order, with `CanDial` last and only when true.
    pub fn to_wire(&self) -> String {
        let mut s = String::from("{");
        s.push_str(&format!("\"InboundAudio\":{}", self.inbound_audio.0));
        s.push_str(&format!(",\"OutboundAudio\":{}", self.outbound_audio.0));
        s.push_str(&format!(",\"DirectCapable\":{}", self.direct_capable));
        s.push_str(&format!(",\"HasRelay\":{}", self.has_relay));
        s.push_str(",\"RTSPTemplate\":");
        gojson::push_string(&mut s, &self.rtsp_template);
        s.push_str(&format!(
            ",\"ReportsAvailability\":{}",
            self.reports_availability
        ));
        if self.can_dial {
            s.push_str(",\"CanDial\":true");
        }
        if let Some(r) = self.station_registered {
            s.push_str(&format!(",\"StationRegistered\":{r}"));
        }
        if self.last_register_at > 0 {
            s.push_str(&format!(",\"LastRegisterAt\":{}", self.last_register_at));
        }
        if !self.features.is_empty() {
            s.push_str(",\"Features\":[");
            for (i, f) in self.features.iter().enumerate() {
                if i > 0 {
                    s.push(',');
                }
                gojson::push_string(&mut s, f);
            }
            s.push(']');
        }
        s.push('}');
        s
    }
}

/// Call states a driver may report through [`EventSink::state_changed`]: §4.5/§9.4's wire
/// states exactly, and the only values the Python `CallStateTracker` accepts. `idle` is
/// deliberately absent: it is HA-side only.
pub const STATE_RINGING: &str = "ringing";
pub const STATE_ANSWERED: &str = "answered";
pub const STATE_ACTIVE: &str = "active";
pub const STATE_ENDED: &str = "ended";

/// The sentinel errors of driver.go. The text is also the IPC error code
/// ([`Sentinel::text`]), except where Go reuses it differently (`no_such_call` is not a
/// reply code: the transport falls through to its other handlers).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Sentinel {
    /// An action the device family cannot do at all, as opposed to one that failed.
    NotSupported,
    /// The device says somebody else has the call, or refuses the claim.
    AlreadyAnswered,
    /// The device refused the claim AND its own status says answering from Portico is off.
    ClaimsDisabled,
    /// `ExternalRing` while the driver already holds a different call.
    Busy,
    /// `HangupCall` for a call the driver does not hold.
    NoSuchCall,
}

impl Sentinel {
    pub fn text(self) -> &'static str {
        match self {
            Sentinel::NotSupported => "not_supported",
            Sentinel::AlreadyAnswered => "already_answered",
            Sentinel::ClaimsDisabled => "claims_disabled",
            Sentinel::Busy => "busy",
            Sentinel::NoSuchCall => "no_such_call",
        }
    }
}

/// A Go `error` as drivers return it: a message, optionally wrapping one [`Sentinel`]
/// (`fmt.Errorf("...: %w", driver.ErrX)`, found again with [`DriverError::is`]).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DriverError {
    message: String,
    sentinel: Option<Sentinel>,
}

impl DriverError {
    /// A plain `errors.New(message)` / `fmt.Errorf` without `%w`.
    pub fn new(message: impl Into<String>) -> Self {
        DriverError {
            message: message.into(),
            sentinel: None,
        }
    }

    /// The bare sentinel (`return driver.ErrNoSuchCall`).
    pub fn sentinel(s: Sentinel) -> Self {
        Self::wrapping(s, s.text())
    }

    /// `fmt.Errorf(<message containing %w>, sentinel)`: `message` is the full formatted text.
    pub fn wrapping(s: Sentinel, message: impl Into<String>) -> Self {
        DriverError {
            message: message.into(),
            sentinel: Some(s),
        }
    }

    /// `errors.Is(err, s)`.
    pub fn is(&self, s: Sentinel) -> bool {
        self.sentinel == Some(s)
    }

    pub fn message(&self) -> &str {
        &self.message
    }
}

impl std::fmt::Display for DriverError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for DriverError {}

/// `driverError(failedCode, err)` (cmd/portico-transport/main.go): an action the driver cannot
/// do at all is relayed as `not_supported`, a lost answer as `already_answered`, a refused
/// claim as `claims_disabled`; everything else keeps the method's own failure code.
pub fn driver_error(failed_code: &str, err: &DriverError) -> IpcError {
    let code = if err.is(Sentinel::NotSupported) {
        "not_supported"
    } else if err.is(Sentinel::AlreadyAnswered) {
        "already_answered"
    } else if err.is(Sentinel::ClaimsDisabled) {
        "claims_disabled"
    } else {
        failed_code
    };
    IpcError::new(code, err.message())
}

/// How a driver reports ring/status events back to the call manager (`EventSink`).
pub trait EventSink: Send + Sync {
    /// A new inbound call (a visitor at the door).
    fn ringing(&self, call_id: &str, device_id: &str);
    /// A later transition of a call already reported through `ringing`: one of the `STATE_*`.
    fn state_changed(&self, call_id: &str, state: &str);
    /// The driver's view of its device changed. Transitions only, never once per poll; `reason`
    /// is a short cause when `available` is false, empty when true, and never carries
    /// credentials. Only a driver whose `reports_availability` is true calls it.
    fn availability_changed(&self, device_id: &str, available: bool, reason: &str);

    /// The type assertions on a sink (`ListenStateSink`, `CapabilitiesSink`, `CallReasonSink`):
    /// a sink that implements one returns itself. A wrapper around a sink must forward each.
    fn listen_state_sink(&self) -> Option<&dyn ListenStateSink> {
        None
    }
    fn capabilities_sink(&self) -> Option<&dyn CapabilitiesSink> {
        None
    }
    fn call_reason_sink(&self) -> Option<&dyn CallReasonSink> {
        None
    }
    /// `errorReporter` (cmd/portico-transport): a sink that takes a driver's `driver_error`
    /// (dahua_sip's registration failure). Go's drivers find it by type assertion.
    fn error_reporter(&self) -> Option<&dyn ErrorReporter> {
        None
    }
    /// DEV-811 P3: a sink that takes a call's audio counters at its end (`call_audio_stats`).
    fn call_stats_sink(&self) -> Option<&dyn CallStatsSink> {
        None
    }
}

/// A sink that wants the audio counters of one answered call, once, when the call's audio ends.
/// `stats` is a JSON object (no call id, no device id: the sink stamps those).
pub trait CallStatsSink: Send + Sync {
    fn call_audio_stats(&self, call_id: &str, stats: &serde_json::Value);
}

/// A sink that wants a driver's unrecoverable failure, once: `code` is a short machine word
/// (`registration_failed`), `msg` one line, never a credential.
pub trait ErrorReporter: Send + Sync {
    fn driver_error(&self, device_id: &str, code: &str, msg: &str);
}

/// A sink that wants the second, audio leg's state (the retired gate_sip_observer's listen leg; no driver reports it now).
pub trait ListenStateSink: Send + Sync {
    fn listen_state_changed(&self, device_id: &str, state: &str, reason: &str);
}

/// A sink that wants to hear that a driver's capabilities changed after `configure` returned
/// them. `caps` is the whole new set; reported on changes only.
pub trait CapabilitiesSink: Send + Sync {
    fn capabilities_changed(&self, device_id: &str, caps: &Capabilities);
}

/// A sink that wants to hear WHY a call the driver answered itself failed or ended. `kind` is
/// `answer_failed` or `ended`; `reason` is one short line, never a credential or a URL.
pub trait CallReasonSink: Send + Sync {
    fn call_reason(&self, device_id: &str, call_id: &str, kind: &str, reason: &str);
}

/// The audio of one call a driver answered itself, as 16 kHz little-endian L16 mono. `input`
/// accepts frames of any even length; `output` yields 1280-byte (40 ms) frames. The driver
/// streams silence to the device while no client is attached; `attach` says a client leg has
/// taken over the pair, `detach` that it is gone, which ends the call.
///
/// Go's `Out()` is a `<-chan` any number of readers can share; here it is one shared receiver
/// behind an async mutex, which keeps that property.
pub trait CallAudio: Send + Sync {
    fn input(&self) -> mpsc::Sender<Vec<u8>>;
    fn output(&self) -> Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>>;
    fn attach(&self);
    fn detach(&self);
}

/// A driver that can answer a call itself and carry its audio.
pub trait AudioClaimer: Send + Sync {
    /// The audio of `call_id` if this driver answered it and still holds it.
    fn claimed_audio(&self, call_id: &str) -> Option<Arc<dyn CallAudio>>;
}

/// A driver that can end a call it answered; `NoSuchCall` for any other call.
pub trait CallHangupper: Send + Sync {
    fn hangup_call(&self, call_id: &str) -> Result<(), DriverError>;

    /// `hangup_call` for a driver whose hang-up has to wait for an answer (Go's `HangupCall`
    /// blocks, up to 3 s, on gate_sip_observer): the hub calls this one. The default is the
    /// synchronous method.
    fn hangup_call_async<'a>(&'a self, call_id: &'a str) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move { self.hangup_call(call_id) })
    }
}

/// `hangup_call` plus the short reason the caller gave, kept in the call's end note.
pub trait ReasonedHangupper: Send + Sync {
    fn hangup_call_why(&self, call_id: &str, why: &str) -> Result<(), DriverError>;
}

/// A driver that can place a call and follow it. The hub mints `call_id` and registers it
/// with the event sink BEFORE `dial_call` runs; transitions are reported ONLY through
/// [`EventSink::state_changed`], in order, ending with `ended`, never through `ringing`.
pub trait CallDialer: Send + Sync {
    fn dial_call<'a>(
        &'a self,
        ctx: &'a CancelToken,
        call_id: &'a str,
        target: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>>;
}

/// A driver whose video URL has to be minted on demand. The URL can embed a credential, so it
/// is returned to the caller and never logged.
pub trait StreamSourcer: Send + Sync {
    fn stream_url<'a>(&'a self, ctx: &'a CancelToken)
        -> BoxFuture<'a, Result<String, DriverError>>;
}

/// An open send-only audio path to a device.
pub trait TalkbackSink: Send + Sync {
    fn input(&self) -> mpsc::Sender<Vec<u8>>;
    /// Go's `Close` ends the session and WAITS for it; this one only asks (it is called from
    /// synchronous code under the driver's locks): [`TalkbackSink::closed`] resolves when the
    /// resources (a subprocess) are released.
    fn close(&self) -> Result<(), DriverError>;
    /// Resolves once the sink is closed and everything it held is released. A sink with
    /// nothing to wait for resolves at once.
    fn closed(&self) -> BoxFuture<'_, ()> {
        Box::pin(async {})
    }
}

/// A driver whose rings are answered by opening a send-only talkback session.
pub trait TalkbackAnswerer: CallHangupper + Send + Sync {
    fn ringing_call(&self, call_id: &str) -> bool;
    fn answer_talkback<'a>(
        &'a self,
        ctx: &'a CancelToken,
        call_id: &'a str,
    ) -> BoxFuture<'a, Result<Arc<dyn TalkbackSink>, DriverError>>;
}

/// A driver that answers a ring itself and carries its audio in-process, but only once a
/// client leg has connected. `answer_with_audio` answers it (the device is told only now) and
/// hands back its audio, already taken by the caller: `NoSuchCall` for a call it does not
/// hold, `AlreadyAnswered` for one already taken.
pub trait ConnectAnswerer: CallHangupper + Send + Sync {
    fn ringing_call(&self, call_id: &str) -> bool;
    fn answer_with_audio<'a>(
        &'a self,
        ctx: &'a CancelToken,
        call_id: &'a str,
    ) -> BoxFuture<'a, Result<Arc<dyn CallAudio>, DriverError>>;
}

/// A driver whose ring is detected outside the transport. `Busy` for a second, different
/// call; the same `call_id` again is a no-op. The caller has already told the phones, so the
/// driver must NOT report it through [`EventSink::ringing`].
pub trait ExternalRinger: Send + Sync {
    fn external_ring(&self, call_id: &str, hold: Duration) -> Result<(), DriverError>;
}

/// `pendingRinger` (cmd/portico-transport/main.go): a driver that can hand over its
/// currently-ringing INVITE for relay-mode answering (dahua_sip). Both methods CONSUME the ring:
/// whichever caller takes it first wins, the next sees none. `pending_ring_for` takes it only
/// when it is `call_id`'s; `pending_ring` takes whichever is there.
pub trait PendingRinger: Send + Sync {
    fn pending_ring(&self) -> Option<IncomingRequest>;
    fn pending_ring_for(&self, call_id: &str) -> Option<IncomingRequest>;
    /// Is `call_id`'s ring pending right now? Consumes nothing (the wsmedia gate asks).
    fn has_pending_ring_for(&self, _call_id: &str) -> bool {
        false
    }
}

/// `TalkHealth`: the door's `/health`-like block: what actually crossed, so a dead audio path
/// cannot read as healthy. Counters are for the current call, or the last one when none is up.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct TalkHealth {
    pub session_up: bool,
    pub bytes_to_camera: u64,
    pub frames_from_camera: u64,
    pub drops_up: u64,
    pub drops_down: u64,
    pub last_ffmpeg: String,
    /// why the last call failed to open or ended ("" before any; "hung up" for a normal end):
    /// one short line, URL-free
    pub last_end: String,
}

impl TalkHealth {
    /// `json.Marshal(TalkHealth)`: the tags, in declaration order.
    pub fn to_wire(&self) -> String {
        let mut s = format!(
            "{{\"session_up\":{},\"bytes_to_camera\":{},\"frames_from_camera\":{},\"drops_up\":{},\"drops_down\":{},\"last_ffmpeg\":",
            self.session_up,
            self.bytes_to_camera,
            self.frames_from_camera,
            self.drops_up,
            self.drops_down
        );
        crate::gojson::push_string(&mut s, &self.last_ffmpeg);
        s.push_str(",\"last_end\":");
        crate::gojson::push_string(&mut s, &self.last_end);
        s.push('}');
        s
    }
}

/// `talkHealther` (cmd/portico-transport/talk_health.go): a driver with a talk diagnostics block.
pub trait TalkHealther: Send + Sync {
    fn health(&self) -> TalkHealth;
}

/// The Go-side extension point a new device family implements (design doc §4.2).
pub trait Driver: Send + Sync {
    fn configure(&mut self, cfg: &Map<String, Value>) -> Result<(), DriverError>;
    fn capabilities(&self) -> Capabilities;
    /// Go's `Capabilities()` may WAIT (gate_sip_observer with dial_enabled waits, bounded, for
    /// its first poll); `capabilities` here is synchronous, so the hub awaits this first and then
    /// reads `capabilities`. Most drivers have nothing to wait for.
    fn capabilities_ready(&self) -> BoxFuture<'_, ()> {
        Box::pin(async {})
    }
    fn start<'a>(
        &'a self,
        ctx: &'a CancelToken,
        events: Arc<dyn EventSink>,
    ) -> BoxFuture<'a, Result<(), DriverError>>;
    fn stop(&self) -> BoxFuture<'_, Result<(), DriverError>>;
    fn dial<'a>(
        &'a self,
        ctx: &'a CancelToken,
        target: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>>;
    fn answer<'a>(
        &'a self,
        ctx: &'a CancelToken,
        target: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>>;
    fn open_relay<'a>(&'a self, ctx: &'a CancelToken) -> BoxFuture<'a, Result<(), DriverError>>;

    /// `deviceIDer` (main.go): the id the hub keys the driver by, when it is not a plain
    /// `host` config field; `None` (or empty) falls back to `host` / `nvr_host`.
    fn device_id(&self) -> Option<String> {
        None
    }

    /// The address a SIP listener is bound to (Go's tests call `d.Server().SIPAddr()`); `None`
    /// for a driver without one. A test seam.
    #[cfg(feature = "test-seams")]
    fn sip_addr(&self) -> Option<std::net::SocketAddr> {
        None
    }

    // The optional interfaces, found by type assertion in Go.
    fn as_audio_claimer(&self) -> Option<&dyn AudioClaimer> {
        None
    }
    fn as_call_hangupper(&self) -> Option<&dyn CallHangupper> {
        None
    }
    fn as_reasoned_hangupper(&self) -> Option<&dyn ReasonedHangupper> {
        None
    }
    fn as_call_dialer(&self) -> Option<&dyn CallDialer> {
        None
    }
    fn as_stream_sourcer(&self) -> Option<&dyn StreamSourcer> {
        None
    }
    fn as_talkback_answerer(&self) -> Option<&dyn TalkbackAnswerer> {
        None
    }
    fn as_connect_answerer(&self) -> Option<&dyn ConnectAnswerer> {
        None
    }
    fn as_external_ringer(&self) -> Option<&dyn ExternalRinger> {
        None
    }
    fn as_pending_ringer(&self) -> Option<&dyn PendingRinger> {
        None
    }
    fn as_talk_healther(&self) -> Option<&dyn TalkHealther> {
        None
    }
}

/// A driver constructor, the value type of Go's `Registry`.
pub type Constructor = fn() -> Box<dyn Driver>;

/// `driver.Registry`: driver key to constructor. Go fills it from `init()` of every imported
/// driver package; here the drivers that exist in transport-rs are listed (four since the observer was retired, DEV-518 C5).
pub fn registry() -> HashMap<&'static str, Constructor> {
    let mut m: HashMap<&'static str, Constructor> = HashMap::new();
    m.insert(dahua::KEY, dahua::new);
    m.insert(gatesipserver::KEY, gatesipserver::new);
    m.insert(unifiprotect::KEY, unifiprotect::new);
    m.insert(unifiprotect::talk::KEY, unifiprotect::talk::new);
    m
}

#[cfg(test)]
mod tests {
    use super::*;

    // TestCanDialIsOmittedWhenFalseAndNamedExactlyWhenTrue (capabilities_golden_test.go)
    #[test]
    fn can_dial_is_omitted_when_false_and_named_exactly_when_true() {
        let off = Capabilities::default().to_wire();
        assert!(
            !off.contains("CanDial"),
            "a false CanDial is on the wire: {off}"
        );
        let on = Capabilities {
            can_dial: true,
            ..Capabilities::default()
        }
        .to_wire();
        assert!(
            on.ends_with(",\"CanDial\":true}"),
            "a true CanDial is not the last key, spelt CanDial: {on}"
        );
        let back: Value = serde_json::from_str(&on).unwrap();
        assert_eq!(back["CanDial"], Value::Bool(true));
    }

    #[test]
    fn sentinel_texts_are_the_ipc_codes() {
        assert_eq!(Sentinel::NotSupported.text(), "not_supported");
        assert_eq!(Sentinel::AlreadyAnswered.text(), "already_answered");
        assert_eq!(Sentinel::ClaimsDisabled.text(), "claims_disabled");
        assert_eq!(Sentinel::Busy.text(), "busy");
        assert_eq!(Sentinel::NoSuchCall.text(), "no_such_call");
        assert_eq!(
            DriverError::sentinel(Sentinel::Busy).to_string(),
            "busy",
            "a bare sentinel prints as its text"
        );
    }

    // driverError (main.go): wrapped sentinels pick the IPC code, everything else keeps the
    // method's own failure code.
    #[test]
    fn driver_error_maps_sentinels_to_ipc_codes() {
        let wrapped = DriverError::wrapping(Sentinel::NotSupported, "x: not_supported (y)");
        let e = driver_error("dial_failed", &wrapped);
        assert_eq!(
            (e.code.as_str(), e.message.as_str()),
            ("not_supported", "x: not_supported (y)")
        );
        for (s, code) in [
            (Sentinel::AlreadyAnswered, "already_answered"),
            (Sentinel::ClaimsDisabled, "claims_disabled"),
            (Sentinel::Busy, "answer_failed"),
            (Sentinel::NoSuchCall, "answer_failed"),
        ] {
            assert_eq!(
                driver_error("answer_failed", &DriverError::sentinel(s)).code,
                code
            );
        }
        assert_eq!(
            driver_error("dial_failed", &DriverError::new("boom")).code,
            "dial_failed"
        );
    }

    // A handler holds `&dyn Trait` across an await in a spawned task: every optional trait
    // must be Send + Sync for that to compile.
    #[tokio::test]
    async fn optional_traits_can_be_held_across_an_await_in_a_spawned_task() {
        fn assert_send<T: Send>(_: &T) {}
        async fn hold(d: Arc<dyn Driver>) {
            let a: Option<&dyn AudioClaimer> = d.as_audio_claimer();
            let h: Option<&dyn CallHangupper> = d.as_call_hangupper();
            let r: Option<&dyn ReasonedHangupper> = d.as_reasoned_hangupper();
            let c: Option<&dyn CallDialer> = d.as_call_dialer();
            let s: Option<&dyn StreamSourcer> = d.as_stream_sourcer();
            let t: Option<&dyn TalkbackAnswerer> = d.as_talkback_answerer();
            let k: Option<&dyn ConnectAnswerer> = d.as_connect_answerer();
            let e: Option<&dyn ExternalRinger> = d.as_external_ringer();
            let p: Option<&dyn PendingRinger> = d.as_pending_ringer();
            let th: Option<&dyn TalkHealther> = d.as_talk_healther();
            tokio::task::yield_now().await;
            let _ = (a, h, r, c, s, t, k, e, p, th);
        }
        let d: Arc<dyn Driver> = Arc::new(gatesipserver::GateSipServer::default());
        let fut = hold(d);
        assert_send(&fut);
        tokio::spawn(fut).await.unwrap();
    }

    #[test]
    fn the_registry_knows_the_ported_drivers() {
        let r = registry();
        assert!(r.contains_key("unifi_protect"));
        assert!(r.contains_key("dahua_sip"));
        assert!(r.contains_key("gate_sip_server"));
        assert!(!r.contains_key("gate_sip_observer"));      // retired, DEV-518 C5
        assert!(r.contains_key("unifi_protect_talk"));
        assert_eq!(r.len(), 4);
    }
}
