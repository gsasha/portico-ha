//! Go standard-library look-alikes shared by the whole crate (R1-55): the text, the error texts and
//! the edge cases of the Go functions the port must answer like, each checked against the real
//! Go function (the golden files) where the behaviour is observable on the wire.
//!
//! * [`strconv`]: `Quote` (`%q`), `Atoi`, `ParseBool`.
//! * [`dial`]: `net.Dialer` (`dialParallel`, `dialSerial`); [`http1`]: the `net/http` client the drivers use (GET, plain or TLS).
//! * [`net`]: `SplitHostPort`, `JoinHostPort`, `localHost`, `ResolveUDPAddr`.
//! * [`tls`]: `crypto/tls` as the drivers use it (system roots, `InsecureSkipVerify`, Go's certificate error texts; R3B-8: it lived in `driver::unifiprotect`).
//! * [`text`]: Go's lossy view of invalid UTF-8, `strings.ToLower`.
//! * [`url`]: `net/url` `Parse` and the pieces of `URL` the drivers read (host, port, request URI).
//! * [`parse`]: `time.ParseDuration`, `Duration.String`, `strconv.ParseFloat`.

pub mod dial;
pub mod http1;
pub mod net;
pub mod parse;
pub mod strconv;
pub(crate) mod text;
pub mod tls;
pub mod url;
