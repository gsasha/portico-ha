//! Rust port of the Go `portico-transport` binary (DEV-764): the IPC server and the request
//! dispatcher, the drivers (`gate_sip_server`, `gate_sip_observer`, `dahua_sip`, `unifi_protect`
//! and its talk family), the SIP, RTP and WebRTC media paths, the house-call bridge, the phone
//! WebSocket media path and the standalone mode. See README.md, DESIGN.md and PARITY.md.
//!
//! Layout: `ipc` and `dispatch` are the wire and the hub; `driver` holds the driver trait family
//! and one directory per driver; `sip`, `sfu`, `call`, `pacer`, `webrtc`, `wsmedia` and `house`
//! are the media and call paths; `process` groups what the binary does around them (flags, panic
//! hook and log scrubber, secret files, standalone mode); `gostd` and `util` are the shared
//! helpers (Go standard-library look-alikes, lock, random, base64); `seams` is the test-only
//! hook set behind the `test-seams` feature.

#![cfg_attr(not(test), deny(clippy::print_stdout, clippy::print_stderr))]

pub mod call;
pub mod cancel;
pub mod dispatch;
pub mod driver;
pub mod evqueue;
pub mod gojson;
pub mod gostd;
pub mod guard;
pub mod house;
pub mod ipc;
pub mod pacer;
pub mod process;
pub use process::{flags, panic_hook, secretfile, secretsenv, standalone};
pub mod seams;
pub mod sfu;
pub mod sip;
pub mod util;
pub mod webrtc;
pub mod wsmedia;
