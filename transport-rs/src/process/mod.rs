//! The process-level modules: what the binary does around the drivers rather than for them.
//!
//! * [`flags`]: the nine command-line flags (Go's `flag` package), signal names, usage text.
//! * [`panic_hook`]: the panic hook, the secret registry and the non-panicking `out_line` /
//!   `err_line` every log line goes through.
//! * [`scrub`]: the URL scrubber a log line, a reason or a panic text passes through.
//! * [`secretfile`] / [`secretsenv`]: the owner-only secret files and the `.env` reader.
//! * [`standalone`]: the `-listen` / `-sip-from-env` mode.
//!
//! They are re-exported at the crate root (`transport_rs::flags`, ...), where they have always
//! been.

pub mod flags;
pub mod panic_hook;
pub mod scrub;
pub mod secretfile;
pub mod secretsenv;
pub mod standalone;
