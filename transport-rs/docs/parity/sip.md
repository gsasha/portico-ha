# transport-rs parity: SIP library

Scope: `pkg/sip` (`src/sip/`): messages, digest, dialog and SDP, registrar, endpoint and its timers, RTP and jitter buffer, plus the Go-compat helpers of `src/gostd/`, `src/gojson.rs` and `src/util.rs`. (The pacer's differences 124 to 126 are in [unifi_protect.md](unifi_protect.md).)

The numbered differences of the whole port are indexed in [../../PARITY.md](../../PARITY.md); their numbers are stable (code comments, tests, the review log and the contract map cite them), so a new difference takes the next free number of its range and goes into the file of its subject. "Smoke" means `scripts/ipc_smoke.py` gave byte-identical output on both binaries, "unit" means a `cargo test` case. Reference: Go `transport/` at the branch point of `dev-764-rust-skeleton` (main `a52af72`), built with `golang:1.23`. Long table cells are shortened to a summary with the full text below the table.

## pkg/sip (slice 2)

Reference: Go `transport/pkg/sip` at the branch point of `dev-764-rust-sip`. Evidence column:
"golden" = a vector produced by the real Go code and replayed in `tests/golden.rs`; "port" = a
ported Go test (Go name in a comment in the Rust test). All 37 Go test functions are ported (the
8 sub-cases of `TestJitterBuffer_Admit` and the 2 of `TestRegistrar_DefinitiveFailureIsNotRetried`
as loops in one test each); none was left out. Golden vectors: Go and Rust agree on every one of
the 87 parse inputs, 45 SDP, 26 RTP, 81 digest, 29 jitter and 60 wire cases; the only vectors
compared more loosely are the `lossy_err` ones (difference 3 below); since round 2 (R2-112) those compare the error text too, `%q` writes an invalid byte as `\xNN` (`go_quote_bytes`).

### Matched

| Area | Behaviour | Evidence |
|---|---|---|
| Parsing | status/request line split on single spaces into at most 3 parts; version check is a prefix test (`SIP/2.0xyz` passes); `Atoi` rules and error text (`+200` ok, `2_00`, `-1`, 20 digits out of range, int64 max); `%q` text in errors [...] (full text below the table) | golden (87 inputs x 2 parsers) |
| SDP | `c=IN IP4 ` (exact, case-sensitive) and `m=audio ` (one space) prefixes, `Fields` splitting on any Unicode space, last line wins, port 0 or empty host rejected while negative and over-65535 ports pass, error texts; `BuildSDP` text | golden |
| Digest, client | response MD5 with and without `qop=auth` (`nc` as `%08x`), header layout (`algorithm=MD5` always, `opaque` last), challenge parsing character by character (quotes toggle, `=` only splits while reading the key, later duplicate key wins, `qop` containing `auth` including `auth-int` means `auth`), algorithm check (`MD5` case-insensitive, trimmed), error texts; the `qop` header is reproduced exactly from the cnonce Go drew | golden (50), port |
| Digest, server | the `(\w+)="([^"]*)"` field scan (ASCII `\w`, keys lower-cased, later duplicate wins), the four required fields, stale before bad, response compared as text (case-sensitive), `qop` forms not understood; nonce TTL 5 min, store capped at 1024 with oldest-expiry eviction | golden (31), port |
| RTP | 12-byte header, `0x80`, marker, PT masked to 7 bits; parse errors and texts; CSRC skipped; padding and extension bits ignored (extension bytes end up in the payload) | golden (26), port |
| Jitter buffer | earliest-in-window start, ordering, duplicates (last wins before playout, late ones purged), gap and too-late silence with the configured byte, wraparound, window of `max(3*delay/ptime, 8)` with the exact edges, resync after 5 near-by outliers (forward, backward, across wrap), scattered outliers never resync, output queue of 8 dropping when full, `Close` stops playout | golden (29 scripts), port |
| Wire | REGISTER (with and without Authorization, de-register), INVITE, ACK (2xx and non-2xx), BYE (UAC and UAS side), CANCEL builder, `Respond` (Via copies, tag rule, body/Content-Length) byte for byte; `Dial`/`AnswerIncoming`/`Registrar` results and error texts | golden (60 scenarios, 120 datagrams) |
| Endpoint | one connected UDP socket, branch correlation, 4096-byte read buffer, off-read-loop handlers serialised per Call-ID, retransmission (T1 doubling, T2 cap for non-INVITE only), 1xx stops Timer A and, for INVITE, restarts Timer C, CANCEL + 487/200-crossed handling, non-2xx ACK; defaults T1 500 ms, T2 4 s, send timeout 5 s, Timer C 3 min, cancel wait 2 s | port (5 timer tests, dispatch test) |
| Registrar | initial retry with backoff (5 s doubling to 5 min, +/-20%), 50% of granted Expires (minimum 1 s, `Duration` overflow wraps as in Go), de-register on cancel, definitive 486/401/403/407 ends `Run`, CSeq advances by 2 per cycle | port (8 tests) |
| AudioSession | seq and timestamp start at 0 and advance by 1 and 640, PT 96, SSRC random, no marker; only the negotiated host is accepted (port ignored); 80 ms playout delay; `Close` idempotent and closes `AudioOut` | port (7 tests) |

**Row Parsing, Behaviour column, in full.** status/request line split on single spaces into at most 3 parts; version check is a prefix test (`SIP/2.0xyz` passes); `Atoi` rules and error text (`+200` ok, `2_00`, `-1`, 20 digits out of range, int64 max); `%q` text in errors; header name trimmed and lower-cased (Unicode), value trimmed (Unicode spaces); lines without a colon skipped; duplicates keep arrival order; blank line = a line of only `\r`/`\n`; LF-only, CR-only, mixed endings; no blank line / no final newline = empty body; Content-Length ignored (body is everything after the blank line, even 16000 bytes against `Content-Length: 5`); no folding, no compact forms; first line without `\n` is an error (`reading status line: EOF`)

### Deliberate differences

| # | Go | Rust | Why |
|---|---|---|---|
| 1 | blocking calls, goroutines, `func(*IncomingRequest)` handlers, `context.Context` | async API on tokio: `Endpoint::new`, `send_request`, `dial`, `answer_incoming`, `respond`, `Registrar::run(&CancelToken)`; handlers are `async` closures; `IncomingRequest` is a value (`to_tag` is a public field, set it before moving the request on) | no goroutines; slice 1 runs a current-thread runtime, so nothing may block a thread (DESIGN.md) |
| 2 | timers are package variables (`timerT1`, ...) | `Timers` per endpoint (`Endpoint::set_timers`) | tests shrink them without a global; same defaults |
| 3 | header values and request lines keep invalid UTF-8 bytes | the string fields (`headers`, `uri`, `reason`...) hold each invalid byte as one U+FFFD (what Go's JSON encoder prints), but the bytes as received are also kept (`raw_headers`, `raw_uri`, `header_raw()`) and **every echoed field is written from them** [...] (full text below the table) | Rust strings are UTF-8; the echo is what a station sees |
| 4 | a panic in a handler goroutine crashes the process | each handler runs in its own task; a panic is contained and the Call-ID queue keeps draining | one bad handler must not wedge a dialog; panic policy is a Phase 2 decision |
| 5 | `JitterBuffer.Out()` is never closed; a reader blocks forever after `Close` | `recv()` returns `None` once closed and drained | no leaked reader; `AudioSession` ends `AudioOut` explicitly as Go does |
| 6 | `SetRemote` twice starts a second pair of loops | second call returns an error | would race on the one `AudioIn` |
| 7 | writing to `AudioIn` after the session closed blocks (nobody reads) | `send` errors | no leak |
| 8 | the RTP socket is `ListenUDP("udp", :port)`, dual-stack | `0.0.0.0` (IPv4 only) | SDP here is `c=IN IP4` only (`ParseSDP` ignores IPv6 `c=` lines); avoids needing `socket2` for `IPV6_V6ONLY` |
| 9 | OS and DNS error text is Go's (`write udp A->B: write: connection refused`) | `gojson::io_err` text (`connection refused`) without the `write udp A->B:` prefix; DNS failures print the OS message | not reproducible without Go's resolver; wrapping prefixes (`sip: writing request: ...`) are kept |
| 10 | challenge keys that differ only in case (`Realm`, `realm`) are applied in random map order | applied in first-appearance order, later value wins | Go is nondeterministic there, no vector can pin it |
| 11 | nonce eviction ties (equal expiry) broken by map iteration order | broken by insertion order | deterministic |
| 12 | `AudioSession` unclosed leaks its goroutines | `Drop` cancels | Rust owner semantics |
| 13 | `unicode.IsPrint` decides what `%q` escapes | exact for ASCII/Latin-1, approximated above (controls, space separators, format characters, private use, noncharacters are escaped; unassigned code points are not) | no Unicode tables without a dependency |
| 14 | `Endpoint.Close` closes the fd and interrupts a blocked read | sets a flag and cancels the read loop; later writes fail with `use of closed network connection` | not identical: the socket is released when the read loop next runs, and a waiting request now ends at once (differences 312, 313) |
| 15 | `Registrar.OnStateChange` / `retryWait` plain fields | `on_state_change` public field, `set_retry_wait` (behind the `test-seams` feature) | tests only |
| 16 | **CANCEL's own `200 OK` is taken for a 200 that crossed the CANCEL** (`endpoint.go:199-211`, `cancelInvite`): Go sends a spurious ACK and BYE and never ACKs the real 487 | the response's CSeq method is checked: a `CANCEL` final is swallowed, a 3xx+ is ACKed with its own To-tag, only a 200 to the INVITE takes the ACK+BYE path. Tests `send_request_cancels_after_timer_c_and_acks_487` (ACK carries the 487's tag, no BYE) and `crossed_200_is_acked_and_torn_down`. No golden vector exercises it (the timers are not settable from outside the Go package), so none needed `known_go_defect`; `tests/golden.rs` supports that flag for a later vector | Go defect |
| 17 | the read loop returns on **any** read error, so an ICMP "port unreachable" on the connected socket (station rebooting) deafens the endpoint for good (`endpoint.go:267`); a refused retransmit aborts `SendRequest` | the loop ends only on close (ConnectionRefused retries at once, other errors after 50 ms); a refused first write is retried once, a refused retransmit is ignored and the timers keep running; `AudioSession`'s send/recv loops likewise survive errors. Tests `endpoint_survives_peer_restart`, `retransmits_survive_refusals_until_the_peer_returns`, `send_loop_survives_a_failed_send` | Go defect |
| 18 | a `SendRequest` abandoned by its caller (hangup mid-ring) sends nothing | a drop guard hands a ringing INVITE to a detached task that sends CANCEL, ACKs the 487 or ACK+BYEs a late 200; an INVITE that never rang is only unregistered. Tests `dropped_*` | Rust futures can be dropped; Go cancels through the caller's own logic |
| 19 | `Call.Bye()` mutates the CSeq (pointer receiver, racy) | `bye(&self)` with an atomic CSeq, so IPC hangup and a remote-BYE handler can share the call with no lock across the up-to-5 s wait | concurrency |
| 20 | `AudioSession.Close` closes the fd synchronously | `close()` is `async`: it cancels the tasks that share the socket, waits for them and drops the socket, so the port is free when it returns (test `close_releases_the_port_immediately`); `Drop` only cancels | a socket shared with tasks cannot be closed from a sync call without unsafe fd tricks |
| 21 | the handler is called on the read-loop goroutine's behalf (a panic kills the process) | the handler is invoked, and its future built, inside the per-Call-ID drain task in its own spawned task: a panic while *building* the future is contained too (test `a_handler_that_panics_synchronously_is_contained`) | difference 4, extended |

**Row 3, Rust column, in full.** the string fields (`headers`, `uri`, `reason`...) hold each invalid byte as one U+FFFD (what Go's JSON encoder prints), but the bytes as received are also kept (`raw_headers`, `raw_uri`, `header_raw()`) and **every echoed field is written from them**: Via, From, To, Call-ID and CSeq in `respond`, and the Request-URI, Via, From, To, Call-ID in ACK/CANCEL/BYE-for-INVITE, so a Latin-1 or GBK byte round-trips unchanged (test `echoed_headers_keep_raw_bytes`). Only `%q` error text for such input differs: vectors with invalid UTF-8 are flagged `lossy_err` and compare only ok/err

### Go oddities (ported, not fixed)

* `readLoop` returns on **any** read error (fixed in Rust, difference 17; listed under defects below).
* `Dial`: a 200 whose SDP does not parse returns an error **without an ACK or a BYE** (the peer
  keeps retransmitting its 200); any other 2xx (202) is "INVITE failed" with no ACK.
* `ParseResponse`/`ParseRequest` ignore `Content-Length`, do not unfold continuation lines (a
  folded line is parsed as a header of its own if it has a colon, else dropped) and do not expand
  compact header names (`v`, `f`, `i`...); lookups use the long names only.
* `ParseRTPPacket` skips the CSRC list but ignores the padding and extension bits, so an
  extension header and padding bytes are returned as payload.
* `AnswerIncoming` leaves the call's CSeq at 0, so the first BYE carries `CSeq: 1`; the UAC BYE
  has a bare-host `To` (`<sip:host>`), the UAS BYE the remote user.
* `SendRequest` registers its channel under the branch, overwriting any request using the same
  branch, and its deferred cleanup removes whatever is registered under it then.
* INVITE retransmission is not capped at T2 (only non-INVITE is), it just stops at the first 1xx
  or the deadline.
* `AuthorizationFor` always writes `algorithm=MD5`, whatever the challenge said; a `qop` that
  merely contains `auth` (`auth-int`) is answered as `auth`.
* `retryDelay(0)` (or a negative count) is 0: `uint(failures-1)` makes the shift count huge.
  Unreachable from `Run`.
* `Registrar.Run` computes the wait as `Duration(granted) * time.Second / 2`, which wraps for an
  absurd `Expires`; ported with wrapping arithmetic.
* Jitter buffer: the playout clock starts at the first push (not at construction), the start
  sequence is the earliest in the first `delay` window, `nextSeq` wraps with `uint16`, and a
  stray packet inside the window but behind `nextSeq` is stored then purged at the next tick.
* `AudioSession` numbers packets from sequence 0 and timestamp 0, never sets the marker, and
  `AudioIn` frames are sent as given (no length check against `RTPFrameBytes`).
* `ParseSDP` accepts a negative or over-65535 port and a payload type of any sign.

### Defects found in the Go code (for a later Go-side fix)

Fixed in the Rust port (differences 16 to 18), still present in `transport/pkg/sip`:

1. `endpoint.go:199-211` `cancelInvite`: the CANCEL's own `200 OK` is treated as a 200 that crossed the CANCEL: spurious ACK and BYE, and the real 487 is never ACKed.
2. `endpoint.go:267` `readLoop` ends on any read error, including ECONNREFUSED from ICMP on the connected socket; `SendRequest` also aborts on a refused retransmit (`endpoint.go` retransmit `conn.Write`). After a station reboot the endpoint reads nothing.
3. `SendRequest` abandoned mid-ring never CANCELs (callers must do it).

Present in both, recorded only (not fixed; slice 3 must account for them):

4. `Dial`: a 200 with unparseable SDP gets neither ACK nor BYE.
5. The SDP host is not validated and is DNS-resolved as given by the network (`SetRemote`).
6. The per-Call-ID queue is unbounded and a retransmitted INVITE is queued and handled again (no de-duplication): slice 3 must de-duplicate.
7. A retransmitted 200 or 487 arriving after `SendRequest` returned is not re-ACKed.
8. The digest response is compared with `!=`, not in constant time (nonce-gated, 5 min TTL).
9. `JitterBuffer::try_recv` (Rust only) returns `None` when a concurrent `recv` holds the lock.

### Not verified

* IPv6 literals and host names for the device or the RTP peer (the `host:port` joining and
  `SplitHostPort` rules are ported; nothing was run against an IPv6 peer).
* The Rust endpoint against a real Dahua station, and trace-diffs of retransmission/pacing timing
  against the Go binary under load (the golden jitter scripts run on a paused clock in Rust and in
  real time in Go; both agree on every output, which says nothing about CPU-starved hosts).
* The ICMP `ECONNREFUSED` path (above) is ported by reading the code, not exercised.
* Unassigned Unicode code points inside `%q` error text (difference 13).

## Phase R round 1 (comments R1-xx of the review log): differences 300 to 369 that belong to this subject

* **310 (R1-4, narrows difference 3's gap): the dialog BYE and the dial ACK echo the peer's bytes.** `Call` keeps the Call-ID, remote tag and the two users as the bytes received (`header_raw`); `build_bye`/`build_ack` take `DialogParams` of bytes. Difference 3 now holds for every echoed field. Tests `uas_bye_keeps_raw_peer_bytes` (mutation-checked).
* **311 (R1-5): the digest client works on bytes.** *(Superseded in its claim about Go by 420: Go does NOT hash and echo the raw bytes.)* `parse_digest_challenge_raw`, `digest_response_raw`, `build_authorization_header_raw`, `authorization_for_raw` hash and echo realm/nonce/opaque as received; `Registrar::register` uses `header_raw`. Expected hashes in the tests were computed with Python hashlib over the raw bytes. The `&str` functions are wrappers. The server verifier has a raw twin (`verify_digest_authorization_raw`, `authorization_nonce_raw`) and `driver/gatesipserver` calls it (`server/registrar.rs`, `server/dial.rs`). Golden vectors: `digest_raw.json` (420).
* **312 (R1-17, corrects difference 14): `Endpoint::close` releases the socket.** It takes `Inner`'s socket handle (the read loop holds the other and drops it when it ends) and empties the handler and pending tables, so a handler closure holding an `IncomingRequest` no longer keeps the fd and port alive. Test `close_releases_the_socket_and_breaks_the_handler_cycle`. Release happens when the read loop next runs, not synchronously inside `close`.
* **313 (R1-121, deliberate difference): `send_request` and the CANCEL wait observe `close`.** After `close` a waiting request returns `Error::Closed` ("use of closed network connection") at once; Go sits out the deadline (5 s, or Timer C). Test `close_ends_a_ringing_request_at_once`.
* **314 (R1-75): out-of-range ports print Go's text** `address 70000: invalid port` (also -1, 65536); confirmed with a Go 1.23 program. No golden vector.
* **315 (R1-76): `cseq_number` clamps** a range error to MaxInt64/MinInt64 like `n, _ := strconv.Atoi`.
* **316 (R1-124): `Error::Timeout` and `Error::Closed`** carry Go's text; nothing on the wire changes.
* **317 (R1-102, R1-103, R1-122):** `AudioSession::close` drops `AudioIn`'s receiver even if `set_remote` never ran (Go blocks); `JitterBuffer::push` is ignored after close; the pacer loop holds a `Weak<Pacer>` so dropping every `Arc` ends it (Go leaks the goroutine).
* **318 (R1-38, partial):** the endpoint read loop has a supervisor (logs a panic, closes the endpoint), locks recover from poisoning, a handler panic logs method and Call-ID. The drain task handle is still not kept; the crate-wide `spawn_observed` is deferred.
* **Declined/accepted:** R1-123 (silence clone per tick, documented).
* **Cleanliness rows of round 1:** R1-55 was done in #316 (`src/gostd/`). The others are decided in round 2 below (R2-133).

## Phase R round 2 (comments R2-xx of the review log): differences 420 to 439 that belong to this subject

* **420 (R2-4, corrects 311, deliberate difference and a Go defect): the digest client hashes and echoes the challenge bytes as received; Go repairs each invalid UTF-8 byte to U+FFFD first.** Go's `splitDigestFields` (`pkg/sip/digest.go:99-111`) does `for _, r := range s` and rebuilds key and value with `WriteRune`, so a realm, nonce or opaque with a Latin-1 byte (`re\xe9alm`) becomes `re\xef\xbf\xbdalm` before the hash and in the echoed `Authorization`. The station hashes its own bytes, so a Go client cannot register against such a station; the Rust client can. Round 1 (311) described this wrongly as parity. The server side is parity (Go's regexp returns the original bytes). Evidence: `tests/golden/digest_raw.json` (flag `known_go_defect`, generated by `GEN_ONLY=digest_raw`, two runs byte-identical) holds the REAL Go answer to three non-UTF-8 challenges; `golden_digest_raw_is_a_known_go_defect` pins that Rust differs from it AND that Rust over the challenge with each invalid byte repaired to U+FFFD is byte-identical to Go's answer, so the repair is the only difference. Hash tests over raw bytes (hashlib): `challenge_with_non_utf8_bytes_is_hashed_and_echoed_raw`, `register_answers_a_non_utf8_challenge_over_raw_bytes`. The error texts that quote the header (`not a Digest challenge`, `incomplete Digest challenge`) print the received bytes as `\xNN`; the one that quotes a repaired value (`unsupported digest algorithm`) prints U+FFFD raw, as Go does.
* **421 (R2-5, matched, not a difference): `serde_json` has `float_roundtrip` in the shipped build.** Before, only the dev-dependency had it and Cargo unifies features inside a test build, so the tests parsed numbers inside a `map[string]any` field exactly (as Go's `ParseFloat`) while the release binary parsed them best-effort (up to 1 ULP off for a literal of more than about 15 significant digits). It is now in `[dependencies]` (the dev line is gone). Checked with `cargo tree -e features` for the non-dev build (see the round-2 log).
* **422 (R2-64, a limit, deliberate): `parse_hex_float` is not Go's algorithm.** It accumulates the mantissa in `f64` and scales with `powi`: a mantissa of more than 53 bits can round twice, a subnormal result can lose bits, and an exponent that does not fit `i32` is refused where Go caps it. Reachable only from the observer's `interval` given as a hex float. Recorded in the function comment.
* **R2-112 closes the `%q` gap of difference 3:** `go_quote_bytes` writes each invalid byte as `\xNN`; the parse and SDP error texts quote the bytes received, and the replay compares the error text of the four parse vectors and the SDP vectors that were `lossy_err` before.
* **Fixed to match Go (no number, each with a test):** `split_host_port("")` has no `address X:` prefix (R2-60); `is_print` has the missing format-character and noncharacter ranges (R2-61); a JSON key folds like `bytes.EqualFold` (`\u017f` and `\u212a`, R2-62); a repeated map field merges into the existing map and a later `null` resets it (R2-63); the CANCEL wait observes `close` also when the response channel ends first (R2-84, difference 313).
* **R2-85 declined:** a request STARTED on a closed endpoint keeps `sip: writing request: use of closed network connection` (Go's `fmt.Errorf("sip: writing request: %w", err)` over `net.ErrClosed`); `tests/dahua_driver.rs` pins that text for a dial after stop, and nothing in `src/` calls `sip::Error::is_closed`. Only a request that was already waiting reports `Error::Closed` (313). Test `a_request_started_on_a_closed_endpoint_has_gos_writing_request_text`.
* **R2-133 decisions.** Done: the stale-doc rows. Declined with a reason: R1-154 (`tick` returning `(Option<Vec<u8>>, Kind)`) and R1-156 (`close` returning a `Result` that is never `Err`) change the public signatures that `unifi_protect`, `dahua` and the observer call, (the first reason given, that those files belonged to other review groups, lapsed with the round-2 merges: R3A-9; open, not declined); the type is honest about what Go's `Close() error` returns, so it stays until all callers move at once. R1-158 (`IncomingRequest` has `Deref` and `pub parsed`): both spellings are used by every driver. R1-159 (headers stored twice, text and raw) is the design of difference 3. R1-160 (`i64` for ports and status): the Go-shaped `int` is what the wire code and the golden replays pass. Deferred, by name: the sleep-based and re-bind tests of R1-125/R1-126 (`tests.rs` 204, 269, 293, 310; `register.rs` `registrar_retries_failed_initial_registration` runs 6 s of wall clock): the retransmit and the re-register cadence now have paused-clock tests (R2-15, R2-16) and these convert the same way; no behaviour is unpinned by them. R2-136 deferred in part: the `&str` twins of the digest functions are kept (a golden replay and about 20 tests pass text; converting them is churn without a behaviour change); the single `find_sub`, the `write!` in `build_sdp`, the `req_method` pair, the mid-file `use` and the typed `AtoiError` are done.
* **R2-135 (layout of `gostd`):** comments fixed and the doubled doc line removed. Deferred: `gostd::time` / `gostd::base64` / `io_err` into `gostd::net` / dropping the `ffmpeg::go_trim_space` re-export. Each is a pure path move, but the importers are in `unifiprotect`, `ipc`, `process` and `wsmedia`, which other round-2 builders are editing now, so the move was to wait for the round-2 merges; they are in (R3A-9) and the move is still not done: it is open, a pure path move with no behaviour change.
* **Tests added for round 2 (mutation-checked in a throwaway edit, then restored):** `invite_retransmits_double_without_a_cap`, `non_invite_retransmits_double_up_to_t2` (R2-15; mutants: no doubling, no T2 cap, INVITE capped), `re_registers_at_half_of_the_granted_expires`, `re_register_interval_has_a_one_second_floor` (R2-16; mutants: 100 %, no floor), `drops_packets_arriving_too_late` extended (R2-17; mutant: no purge), `a_loop_that_wakes_late_resynchronises_without_a_burst` (R2-18; mutant: no resync).

> **Go sources removed (D-20261123-cc1e).** Go paths in this file, such as `transport/...`, `tools/gen_golden/...` and `scripts/golden_check.sh`, name the Go transport and the golden generators as they were at commit `bdd8e61`; they are deleted from `main` and live in git history only. The golden vectors in `tests/golden/` are frozen recordings. See [tests_and_vectors.md](../tests_and_vectors.md).
