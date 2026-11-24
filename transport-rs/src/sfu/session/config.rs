//! The session's settings: the defaults, the `PORTICO_SFU_*` environment, the test-seam `Fault`.

use super::*;

/// How long `Disconnected` may last before the session is over. Go ended a vanished browser on
/// pion's `Failed` (about 30 s after the last packet); see PARITY.md for how this default was
/// chosen from the S1b measurements and the slice 4b measurements.
pub const DEFAULT_DISCONNECT_GRACE: Duration = Duration::from_secs(6);
/// A session whose browser never connects ends after this (pion went `Failed` about 30 s after
/// the answer; str0m would stay in `Checking`).
pub const DEFAULT_CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
/// str0m's own default is used (21.9 to 24.2 s to `Disconnected` in S1b, measured again in
/// INTEROP.md); `PORTICO_SFU_MAX_STUN_RETRANSMITS` overrides it.
pub const DEFAULT_MAX_STUN_RETRANSMITS: Option<usize> = None;

/// A fault injected into a session by the tests (`SessionConfig::fault`); never set in
/// production. Each one panics once the offer was accepted, at a different place: inside
/// `accept_offer` (the `catch_unwind` of the offer), inside a str0m poll (the `guard`), and in
/// the session task outside every guard (the join-handle supervisor).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Fault {
    #[default]
    None,
    PanicInAcceptOffer,
    PanicInPoll,
    PanicInTask,
    /// in `candidate()` (the candidate parser and `add_remote_candidate`), after an offer
    PanicInCandidate,
    /// in the post-processing of the answer (the `sdp::` helpers and `policy::answer_codecs`)
    PanicInAnswerPost,
    /// while handling a received RTP packet (`UplinkPath::on_rtp`, the jitter buffer)
    PanicInRtp,
    /// every UDP receive fails (an injected error source for the reader's backoff)
    RecvError,
    /// `poll_output` keeps returning a `Transmit` (1,000,000 of them, to a source no socket
    /// owns) once an offer was accepted: the stand-in for an `Rtc` that never asks for a timeout
    EndlessTransmit,
    /// while parsing / handling a received datagram, the step that used to sit outside every guard
    PanicInDatagram,
    /// in `negotiated()`, right after the offer was accepted
    PanicInNegotiated,
    /// while adding the host candidates in `bind`
    PanicInBind,
}

/// The most outputs of `poll_output` one pass of the loop takes before it lets the other inputs
/// (and `stop`) have a turn, and the most `goodbye` takes: an `Rtc` that never asks for a timeout
/// must not pin the one thread every session shares (R1-20).
pub(super) const MAX_OUTPUTS_PER_PASS: usize = 512;
/// The most remote candidates one session accepts (R1-39): a browser sends well under ten.
pub const MAX_REMOTE_CANDIDATES: usize = 32;

/// The most concurrent browser sessions (`PORTICO_SFU_MAX_SESSIONS`, clamped to 1..64).
pub const DEFAULT_MAX_SESSIONS: usize = 8;
/// A UDP reader that fails this many times in a row ends its session.
pub const RECV_ERROR_LIMIT: u32 = 50;

#[derive(Clone, Debug)]
pub struct SessionConfig {
    /// test seam: a fault injected into the session (see [`Fault`]); a shipped build has none
    #[cfg(feature = "test-seams")]
    pub fault: Fault,
    pub ifaces: IfacePolicy,
    pub max_stun_retransmits: Option<usize>,
    pub disconnect_grace: Duration,
    pub connect_timeout: Duration,
    /// the registry refuses a new call id beyond this many live sessions
    pub max_sessions: usize,
    /// the ceiling of the UDP reader's error backoff (10 ms doubling up to this)
    pub recv_backoff_max: Duration,
    /// `PORTICO_SFU_TRACE_RTP=1`: one line per received RTP packet (diagnosis only)
    pub trace_rtp: bool,
}

impl SessionConfig {
    /// Whether the test-seam fault `f` is injected. Always false in a shipped build.
    #[cfg(feature = "test-seams")]
    #[inline]
    pub(super) fn fault_is(&self, f: Fault) -> bool {
        self.fault == f
    }

    #[cfg(not(feature = "test-seams"))]
    #[inline(always)]
    pub(super) fn fault_is(&self, _f: Fault) -> bool {
        false
    }
}

impl Default for SessionConfig {
    fn default() -> Self {
        SessionConfig {
            #[cfg(feature = "test-seams")]
            fault: Fault::None,
            ifaces: IfacePolicy::default(),
            max_stun_retransmits: DEFAULT_MAX_STUN_RETRANSMITS,
            disconnect_grace: DEFAULT_DISCONNECT_GRACE,
            connect_timeout: DEFAULT_CONNECT_TIMEOUT,
            max_sessions: DEFAULT_MAX_SESSIONS,
            recv_backoff_max: Duration::from_millis(500),
            trace_rtp: false,
        }
    }
}

impl SessionConfig {
    /// The defaults, overridden by `PORTICO_SFU_EXCLUDE_IFACES`, `PORTICO_SFU_INCLUDE_LOOPBACK`,
    /// `PORTICO_SFU_MAX_STUN_RETRANSMITS`, `PORTICO_SFU_MAX_SESSIONS`,
    /// `PORTICO_SFU_DISCONNECT_GRACE_SECS`, `PORTICO_SFU_CONNECT_TIMEOUT_SECS` and
    /// `PORTICO_SFU_TRACE_RTP`. This is the only place the environment is read; a value that
    /// is not usable is named on stderr and the default stays.
    pub fn from_env(getenv: impl Fn(&str) -> Option<String> + Copy) -> Self {
        let bad = |k: &str, v: &str| {
            crate::panic_hook::err_line(&format!(
                "portico-transport: {k}={v:?} is not usable, using the default"
            ));
        };
        let mut c = SessionConfig {
            ifaces: IfacePolicy::from_env_with(getenv, bad),
            trace_rtp: getenv("PORTICO_SFU_TRACE_RTP").is_some_and(|v| v == "1"),
            ..SessionConfig::default()
        };
        if let Some(v) = getenv("PORTICO_SFU_MAX_STUN_RETRANSMITS") {
            match v.trim().parse::<usize>() {
                Ok(n) if (1..=20).contains(&n) => c.max_stun_retransmits = Some(n),
                _ => bad("PORTICO_SFU_MAX_STUN_RETRANSMITS", &v),
            }
        }
        if let Some(v) = getenv("PORTICO_SFU_MAX_SESSIONS") {
            match v.trim().parse::<usize>() {
                Ok(n) => c.max_sessions = n.clamp(1, 64),
                _ => bad("PORTICO_SFU_MAX_SESSIONS", &v),
            }
        }
        for (k, slot) in [
            ("PORTICO_SFU_DISCONNECT_GRACE_SECS", &mut c.disconnect_grace),
            ("PORTICO_SFU_CONNECT_TIMEOUT_SECS", &mut c.connect_timeout),
        ] {
            if let Some(v) = getenv(k) {
                match parse_secs(&v) {
                    Some(d) => *slot = d,
                    None => bad(k, &v),
                }
            }
        }
        c
    }

    /// The config the process uses: the environment of this process.
    pub fn from_process_env() -> Self {
        Self::from_env(|k| std::env::var(k).ok())
    }

    /// Loopback only; what the in-process tests use (Go's `hostOnlySettingEngine`).
    pub fn loopback() -> Self {
        SessionConfig {
            ifaces: IfacePolicy::loopback_only(),
            ..SessionConfig::default()
        }
    }
}

/// A number of seconds from the environment: finite and positive, clamped to 1..3600 s (a huge
/// value would overflow `Instant + Duration`; `from_secs_f64` of 1e30 panics).
pub(crate) fn parse_secs(v: &str) -> Option<Duration> {
    let f: f64 = v.trim().parse().ok()?;
    if !f.is_finite() || f <= 0.0 {
        return None;
    }
    Duration::try_from_secs_f64(f.clamp(1.0, 3600.0)).ok()
}
