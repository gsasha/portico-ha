//! The browser-facing WebRTC session: Go's `Session` (`internal/sfu/sfu.go`) on `str0m` 0.25.0.
//!
//! One `Session` per call id. `str0m` is sans-IO, so the session is a tokio task (`run`) that owns
//! the `Rtc`, a UDP socket per allowed local address (see `ifaces`) and four inputs: commands from
//! the IPC handlers (offer, candidate, answer), datagrams from the socket reader tasks, device
//! frames from `AudioIn`, and the timers (str0m's own, the `Disconnected` grace, the connect
//! timeout). The pieces of Go's `Session` map like this:
//!
//! | Go | here |
//! |---|---|
//! | `newSession` (codecs G.722 and PCMU, never Opus) | `Session::new`, `build_rtc` |
//! | `HandleOffer` (non-trickle answer) | `Session::handle_offer` -> `Core::offer` |
//! | `AddICECandidate` | `Session::add_ice_candidate` -> `Core::candidate` |
//! | `readRemoteTrack` | `Event::RawPacket(RtpRx)` -> `UplinkPath::on_rtp` (raw sequence numbers) |
//! | `forwardJitterOut` | `forward_jitter_out` task |
//! | `encodeLoop` | the `audio_in` arm of `run` -> `Core::write_audio` |
//! | `OnConnectionStateChange` | `Event::Connected` / `IceConnectionStateChange` in `Core::event` |
//! | `OnConnected` / `OnDisconnected` / `Close` | same names, fire-once as in Go |
//!
//! Decisions (all recorded in PARITY.md, slice 4b):
//!
//! * The answer sent is str0m's. `policy.rs` (a model of pion) is kept as the cross-check: the
//!   payload types of the str0m answer are compared with `policy::answer_codecs` and a
//!   disagreement is logged, never acted on.
//! * The payload types and the codec sent to the phone are what str0m negotiated
//!   (`Core::negotiated`): the uplink gate is built from them and the send codec is the first
//!   G.722/PCMU entry of the answer's `m=audio` line.
//! * `Disconnected` is not terminal in str0m (there is no `Failed`): it starts a grace timer, a
//!   recovery cancels it, the end of it fires `OnDisconnected` once and closes the session.
//!   A session that never connects ends after `connect_timeout`. Defaults and numbers: PARITY.md.
//! * A panic inside str0m ends THAT session: every call into it is under `catch_unwind` and the
//!   task itself is supervised through its `JoinHandle`.

use crate::util::lock;
use std::net::SocketAddr;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Once};
use std::time::{Duration, Instant};

use str0m::change::SdpOffer;
use str0m::format::Codec;
use str0m::media::{Frequency, MediaKind, MediaTime, Mid, Pt};
use str0m::net::{Protocol, Receive, Transmit};
use str0m::rtp::RawPacket;
use str0m::{Candidate, Event, IceConnectionState, Input, Output, Rtc, RtcConfig, RtcError};
use tokio::net::UdpSocket;
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

use super::audio::{
    DownlinkEncoder, PhoneCodec, RtpSendState, UplinkConverter, UplinkGate, UplinkPath,
};
use super::ifaces::{self, IfacePolicy};
use super::policy;
use super::sdp;
use super::uplink::UplinkBuffer;
use crate::cancel::CancelToken;

mod config;
mod engine;
mod handle;
mod io;
mod report;
mod run;

pub use config::*;
pub use handle::*;

use engine::*;
use io::Reader;
use run::run;

#[cfg(test)]
mod tests;
