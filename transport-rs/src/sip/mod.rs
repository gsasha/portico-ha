//! Port of `transport/pkg/sip` (DEV-764 slice 2): SIP registration, INVITE/BYE, digest
//! authentication, RTP framing and the jitter buffer. Behaviour follows the Go package; every
//! difference is in PARITY.md, the task layout in DESIGN.md.
//!
//! Go name to Rust name: `ParseRequest` `parse_request`, `ParseResponse` `parse_response`,
//! `ParseSDP`/`BuildSDP` `parse_sdp`/`build_sdp`, `NewEndpoint` `Endpoint::new`, `SendRequest`
//! `Endpoint::send_request`, `Dial`/`AnswerIncoming` `dial`/`answer_incoming`, `NewRegistrar`
//! `Registrar::new`, `NonceStore` `NonceStore`, `VerifyDigestAuthorization`
//! `verify_digest_authorization`, `AuthorizationFor` `authorization_for`, `OpenAudioSocket`
//! `AudioSession::open_audio_socket`, `RTPFrameBytes` `RTP_FRAME_BYTES`.

use std::fmt;

pub mod dialog;
pub mod digest;
pub mod endpoint;
pub mod jitter;
pub mod message;
pub mod register;
pub mod rtp;

pub use dialog::{answer_incoming, build_sdp, dial, parse_sdp, Call, SdpInfo};
pub use digest::{
    authorization_for, authorization_nonce, verify_digest_authorization, DigestChallenge,
    DigestResult, NonceStore,
};
pub use endpoint::{Endpoint, IncomingRequest, Timers};
pub use jitter::JitterBuffer;
pub use message::{parse_request, parse_response, ParsedRequest, Response};
pub use register::Registrar;
pub use rtp::{parse_rtp_packet, AudioSession, RtpPacket, RTP_FRAME_BYTES};

/// The errors of this package. `Display` gives Go's error text (without the `%w` wrapping Go
/// adds only for `errors.Is`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// `ErrBusyHere`: the device answered a REGISTER with 486.
    BusyHere,
    /// `ErrAuthRejected`, with the optional `"403 Forbidden"` detail Go appends (`"%w: %d %s"`).
    AuthRejected(String),
    /// A request got no (final) response within its wait: Go's `sip: timed out waiting for a
    /// response`.
    Timeout,
    /// The endpoint was closed under the call: Go's `use of closed network connection`.
    Closed,
    /// Any other error, already formatted as Go formats it.
    Msg(String),
}

impl Error {
    pub fn msg(s: impl Into<String>) -> Self {
        Error::Msg(s.into())
    }

    /// `errors.Is(err, ErrAuthRejected)`.
    pub fn is_auth_rejected(&self) -> bool {
        matches!(self, Error::AuthRejected(_))
    }

    /// Whether the request timed out waiting for a response.
    pub fn is_timeout(&self) -> bool {
        matches!(self, Error::Timeout)
    }

    /// Whether the endpoint was closed.
    pub fn is_closed(&self) -> bool {
        matches!(self, Error::Closed)
    }

    /// `errors.Is(err, ErrBusyHere)`.
    pub fn is_busy_here(&self) -> bool {
        matches!(self, Error::BusyHere)
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::BusyHere => f.write_str("sip: 486 Busy Here (session quota exhausted)"),
            Error::AuthRejected(d) if d.is_empty() => f.write_str(AUTH_REJECTED_TEXT),
            Error::AuthRejected(d) => write!(f, "{AUTH_REJECTED_TEXT}: {d}"),
            Error::Timeout => f.write_str("sip: timed out waiting for a response"),
            Error::Closed => f.write_str("use of closed network connection"),
            Error::Msg(s) => f.write_str(s),
        }
    }
}

impl std::error::Error for Error {}

const AUTH_REJECTED_TEXT: &str = "sip: registration rejected (authentication failed or forbidden)";
