# transport-rs: dependencies

Moved from the README.

## Dependencies

All pinned with `=` (`rustls` 0.23.46 and `tokio-rustls` 0.26.6 since R1-203); `Cargo.lock` is committed.

| Crate | Why |
|---|---|
| `rustls` 0.23 (`std`, `aws_lc_rs`, `tls12`), `tokio-rustls` 0.26, `rustls-pki-types`, `aws-lc-rs` | slice 7a: HTTPS and wss to the Protect console, no OpenSSL and no `ring`; the provider and `rustls-pki-types` were already in the tree (PARITY.md difference 109). `rcgen` (already in the tree) is a dev-dependency |
| `tokio` | runtime, UDP, timers (slice 1; dev-dependency also enables `test-util` for the paused clock in jitter tests); the `process` feature since slice 7b (the `ffmpeg` subprocesses: `kill_on_drop`, one task reaps each) |
| `serde`, `serde_json` | IPC JSON (slice 1) |
| `md-5` 0.10.6 (`default-features = false`) | the digest algorithm (RFC 2617 is MD5); RustCrypto, no other way to be byte-identical to Go's `crypto/md5` |
| `getrandom` 0.2.16 | OS randomness for nonces, tags, branches and the SSRC (Go: `crypto/rand`); one syscall wrapper, no RNG framework pulled in |
| `str0m` 0.25.0 (`default-features = false`, feature `aws-lc-rs`) | the WebRTC session (G1, `spikes/RESULTS_S1b.md`): sans-IO ICE, DTLS, SRTP. Pulls `aws-lc-sys` (C and asm, built by `cc`) and `dimpl`; the release binary grew from 1.08 MB to 4.7 MB (glibc) |
| `if-addrs` 0.13.4 | `getifaddrs` for the host-candidate allow-list (`src/sfu/ifaces.rs`) |

| `socket2` 0.6.5 (feature `all`) | slice 10: TCP keep-alive and no-delay on the accepted IPC connection (Go's `net` sets both), `IPV6_V6ONLY` off on the wildcard bind; already in the tree through tokio, so no new crate |

Not added: `tokio-util` (a 30-line `CancelToken` replaces it), `rand`, `regex`.
