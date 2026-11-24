//! Port of `internal/house` (`house.go`) and `cmd/portico-transport/house.go` (DEV-764 slice 9):
//! the house-call bridge. Two audio legs of one phone-to-phone call joined inside the process,
//! default OFF (`-house-calls`). Off, `house_open` / `house_close` answer `not_supported`
//! (`dispatch/`); on, this module owns a table of bridges of its own and re-registers
//! `media_frame` / `media_close` with a router that sends ids starting with `house-` here and every
//! other id, untouched, to the door path (`wsmedia::WsMediaHub`).
//!
//! A [`Bridge`] owns exactly two legs and two forwarders (from -> to, to -> from). A leg is a
//! frame channel over the signalling socket ([`crate::wsmedia::Leg`]) or a WebRTC session
//! ([`crate::sfu::session::Session`]); both carry 16 kHz L16 40 ms frames (1280 bytes) on the
//! forwarder side. No mixer, no codec work here; a full destination drops the new frame.
//!
//! Rust-specific shape (PARITY.md 162 to 170): ending a call is a synchronous half
//! ([`Bridge::end`]: the flag, the done token, the slots) and an awaited half ([`EndTail`]: close
//! both legs, the counters line, the `lost` events), so a callback that cannot await still frees
//! the slot at once; every `house_leg_state` event goes through one ordered queue and one drain
//! task; every spawned task that carries a call has a panic guard that ends the call and closes
//! both legs.

use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use tokio::sync::{mpsc, Mutex as AsyncMutex};

use crate::util::PushError;

/// One 40 ms L16 16 kHz mono frame.
pub const FRAME_BYTES: usize = 1280;

pub const DEFAULT_MAX_BRIDGES: usize = 4; // HC-5
pub const DEFAULT_MAX_PER_OWNER: usize = 2;
pub const DEFAULT_SILENCE: Duration = Duration::from_secs(10);
/// The call ids the UI part mints for a house call (`house.py` `HOUSE_PREFIX`).
pub const DEFAULT_CALL_PREFIX: &str = "hso-";
/// Starts every id this module mints (session and frame-channel leg ids).
pub const SESSION_PREFIX: &str = "house-";

pub const SIDE_FROM: &str = "from";
pub const SIDE_TO: &str = "to";
pub const KIND_FRAMES: &str = "frames";
pub const KIND_WEBRTC: &str = "webrtc";

pub const STATE_SDP: &str = "sdp";
pub const STATE_ATTACHED: &str = "attached";
pub const STATE_FIRST_FRAME: &str = "first_frame";
pub const STATE_LOST: &str = "lost";

pub const REASON_HANGUP: &str = "hangup";
pub const REASON_LOST: &str = "lost";
pub const REASON_SILENCE: &str = "silence";
pub const REASON_SHUTDOWN: &str = "shutdown";
pub const REASON_WEBRTC_FAILED: &str = "webrtc_failed";
pub const REASON_BRIDGE_CLOSED: &str = "bridge_closed";
/// A task that carries the call panicked (Rust only; Go has no recover here).
pub const REASON_ERROR: &str = "error";

const MAX_OWNER_LEN: usize = 64;
const MAX_CALL_ID_LEN: usize = 128;
/// One frame of audio: 40 ms.
const FRAME_TIME_NS: i64 = 40_000_000;

pub type BoxFut<T = ()> = Pin<Box<dyn Future<Output = T> + Send>>;
type Receiver = Arc<AsyncMutex<mpsc::Receiver<Vec<u8>>>>;

// ---- errors ------------------------------------------------------------------------------------

/// `ErrBadParams`, `ErrTooMany`, `ErrAlreadyOpen`, `ErrShutdown` and the factory's failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HouseError {
    BadParams(String),
    TooMany(String),
    AlreadyOpen,
    Shutdown,
    Failed(String),
}

impl HouseError {
    /// Go's `err.Error()` (wrapped texts included).
    pub fn message(&self) -> String {
        match self {
            HouseError::BadParams(m) => format!("house: bad parameters: {m}"),
            HouseError::TooMany(m) => format!("house: too many house calls: {m}"),
            HouseError::AlreadyOpen => "house: call already open".to_string(),
            HouseError::Shutdown => "house: bridge is shutting down".to_string(),
            HouseError::Failed(m) => m.clone(),
        }
    }
}

/// What [`Manager::frame`] can say: `ErrUnknownLeg` or the leg's own error.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FrameError {
    UnknownLeg,
    Leg(PushError),
}

// ---- legs and the factory ----------------------------------------------------------------------

/// One end of a house call (`house.Leg`).
pub trait HouseLeg: Send + Sync + 'static {
    /// `In()`: L16 frames toward the phone.
    fn input(&self) -> mpsc::Sender<Vec<u8>>;
    /// `Out()`: L16 frames from the phone.
    fn output(&self) -> Receiver;
    /// `cb` runs once when the leg connects (never registered for a frames leg).
    fn on_connected(&self, cb: Box<dyn FnOnce() + Send>);
    /// `cb` runs once when the leg is gone; at once, on the caller's stack, if it already is.
    fn on_disconnected(&self, cb: Box<dyn FnOnce() + Send>);
    /// `Close(reason)`, idempotent. The returned future finishes when the leg's own close
    /// callbacks have run.
    fn close(&self, reason: &str) -> BoxFut;
    /// Resolves when the leg has released its sockets (a WebRTC session's task); the manager
    /// waits for it, bounded, at shutdown. Frames legs have nothing to wait for.
    fn wait_released(&self) -> BoxFut {
        Box::pin(async {})
    }
}

/// A frame-channel leg: the uplink is pushed in by the hub (`media_frame` requests).
pub trait FramesLeg: HouseLeg {
    fn push(&self, payload: &[u8]) -> Result<(), PushError>;
}

/// A WebRTC leg: it answers the phone's offer (gathering ICE first, which can take a while).
pub trait WebRtcLeg: HouseLeg {
    fn handle_offer(&self, sdp: String) -> BoxFut<Result<String, String>>;
}

/// Builds legs. `leg_id` is the id a frames leg carries on `media_frame` / `media_closed`.
pub trait Factory: Send + Sync + 'static {
    fn frames(&self, leg_id: &str, codec: &str) -> BoxFut<Result<Arc<dyn FramesLeg>, String>>;
    fn webrtc(&self, leg_id: &str) -> BoxFut<Result<Arc<dyn WebRtcLeg>, String>>;
}

/// One `house_leg_state` notification.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Event {
    pub call_id: String,
    pub side: String,
    pub state: String,
    /// `lost` only
    pub reason: String,
    /// `sdp` only
    pub sdp: String,
}

/// Receives the events, one at a time, in order.
pub type EmitFn = Arc<dyn Fn(Event) -> BoxFut + Send + Sync>;

/// One leg of `house_open`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LegSpec {
    pub side: String,
    pub kind: String,
    /// frames only: `pcmu` or `l16`
    pub codec: String,
    /// webrtc only: the phone's offer
    pub sdp: String,
    /// optional: whose phone, for the per-owner cap
    pub owner: String,
}

/// What `house_open` tells the caller about one leg.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenedLeg {
    pub side: String,
    pub kind: String,
    /// frames: the id `media_frame` / `media_close` / `media_closed` use
    pub leg_id: String,
    pub codec: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Opened {
    pub session: String,
    pub legs: Vec<OpenedLeg>,
}

/// Bounds a [`Manager`]; a zero field takes its default.
#[derive(Clone, Default)]
pub struct Config {
    pub max_bridges: usize,
    pub max_per_owner: usize,
    pub silence: Duration,
    /// How long `close_all` waits, in all, for the bridges' legs to close; 6 s by default.
    pub close_all_wait: Duration,
    pub call_prefix: String,
    /// Receives the one counters line a call writes when it ends; `None` logs nothing.
    pub logf: Option<Arc<dyn Fn(String) + Send + Sync>>,
    /// The clock the counters read, in nanoseconds since an arbitrary epoch, never 0 (`0` means
    /// "no frame yet"); `None` uses the monotonic clock.
    pub now: Option<Arc<dyn Fn() -> u64 + Send + Sync>>,
    /// The hex part of a session id; `None` uses random bytes. A duplicate is retried.
    pub new_id: Option<Arc<dyn Fn() -> String + Send + Sync>>,
}

fn mono_now() -> u64 {
    use std::time::Instant;
    static T0: OnceLock<Instant> = OnceLock::new();
    T0.get_or_init(Instant::now).elapsed().as_nanos() as u64 + 1
}

fn random_hex() -> String {
    let mut b = [0u8; 8];
    if getrandom::getrandom(&mut b).is_err() {
        return format!("{:016x}", mono_now());
    }
    b.iter().map(|x| format!("{x:02x}")).collect()
}

mod bridge;
mod hub;
mod manager;
mod params;

pub use bridge::{Bridge, EndTail};
pub use hub::HouseHub;
pub use manager::Manager;
pub use params::{decode_open_params, OpenParams};
