//! Port of the Go package `internal/call` (DEV-764 slices 4b and 6): `state_machine.go` and
//! `manager.go` (the per-device call table and its state), `sequencer.go` (never answer the device
//! before the browser leg connected; join the audio both ways once it did) and, since slice 6,
//! `relay.go` (the SIP/RTP device leg of the dahua_sip relay mode) and, since slice 7b, the
//! talkback sink (`UseTalkback`, `OpenTalkbackOnceConnected`, the send-only device leg of
//! `unifi_protect`); the sequencer is generic over the step that
//! answers the device, which is exactly the part of `AnswerViaRelayOnceConnected` that
//! `relay.AnswerDevice` was.

/// `Manager` and `StateMachine` have no production caller, here or in Go's `cmd/` (a ported
/// package nothing uses; `State::Ended` is never set in either): they are kept as ported and
/// tested, not wired.
pub mod manager;
pub mod relay;
pub mod sequencer;
pub mod state_machine;

use std::sync::Arc;

use tokio::sync::mpsc;

pub use manager::{Manager, ManagerError};
pub use relay::{media_host, Relay};
pub use sequencer::{answer_once_connected, forward, open_talkback_once_connected, DeviceLeg};
pub use state_machine::{State, StateMachine};

/// Go's `ClientLeg` plus the `OnDisconnected` of `browserLeg` (main.go): a client-facing leg
/// whose connection completes at some point, whose audio is a pair of frame channels, and which
/// can go away for good.
///
/// Callbacks run on whichever task fires them (the session's own task): they must not block.
pub trait ClientLeg: Send + Sync + 'static {
    /// `cb` runs exactly once when the leg connects; at once if it already is connected.
    fn on_connected(&self, cb: Box<dyn FnOnce() + Send>);
    /// `cb` runs exactly once when the leg is gone for good; at once if it already is.
    fn on_disconnected(&self, cb: Box<dyn FnOnce() + Send>);
    /// `In()`: frames for the client (device audio to the browser).
    fn input(&self) -> mpsc::Sender<Vec<u8>>;
    /// `Out()`: frames from the client.
    fn output(&self) -> Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>>;
}
