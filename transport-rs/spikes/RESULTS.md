# WebRTC library spike: results (DEV-764 S1, S2, S3)

Date of the runs: 2026-11-12. Evidence for the decision at gate G1; the parent decides. Throwaway code,
how to rerun it: [README.md](README.md). Every number below was measured in this session unless it says
**not verified**. Host: 4 cores, Linux 6.1, all peers and answerers are static binaries on 127.0.0.1
(no docker network). Offerer: pion webrtc v4.1.8 / rtp v1.8.26 (the versions in `transport/go.mod`),
static, `CGO_ENABLED=0`, built in `golang:1.23-alpine`. Answerers: `str0m =0.25.0` and `webrtc =0.21.1`
(over `rtc =0.21.1`), x86_64-unknown-linux-musl release builds (LTO, stripped).

## 1. Verdict in one table

| Question | str0m 0.25.0 | webrtc-rs 0.21.1 |
|---|---|---|
| Interop with pion offerer, 60 s, tone both ways | PASS, 0 lost, tone ratio 1.00 both ways | PASS, 0 lost, tone ratio 1.00 both ways |
| 30-minute run | PASS: 89,988 pkts each way, 0 lost, RSS flat 5.76 to 5.81 MB, 0.87 % CPU, no panic | PASS: 89,990 / 89,989 pkts, 0 lost, RSS 6.30 to 6.81 MB (two small steps), 1.15 % CPU, no panic |
| ICE restart (answerer side, 2 restarts) | seamless: 10 ms to re-complete, 0 to 1 packet lost per restart | audio gap of about 1.0 s per restart (50 packets each way) |
| Peer vanishes (SIGSTOP 12 s) | no state change on the answerer; audio resumed on its own | `Disconnected` after about 4 to 5 s, `Connected` again at resume; audio resumed |
| Peer vanishes for good (50 s) | one `Disconnected` after about 22 s; no `Failed` state exists | `Disconnected` after about 4 s, `Failed` after about 29 s |
| Two sessions in one process | works (my choice: one thread each) | works (one tokio runtime) |
| Offer with `a=rtpmap:0 PCMU/8000/1` | **rejected** (`m=audio 0`), no audio | accepted, audio flows |
| Static musl x86_64 / aarch64 build | both build, 6.46 MB / 4.79 MB | both build, 5.39 MB / 4.66 MB |
| Crypto in the graph | aws-lc-rs (C and asm) by default; the `rust-crypto` feature still pulls aws-lc through `dimpl` | `ring` by default (C and asm); `aws-lc-rs` by feature |
| C compiler needed | yes (aws-lc-sys, via `cc`; no cmake) | yes (ring, via `cc`) |
| Unique crates (normal deps, musl) | 86 | 179 (includes tokio) |
| In-call RSS, 1 session | 5.8 MB | 6.6 MB |
| Releases in 12 months / distinct authors in 6 months | 22 / 43 | 12 stable and 15 pre-releases / 14 (the sans-IO core `rtc`: 28 stable / 43) |
| Median hours to a maintainer's first reply, latest issues | 21 (9 of 13 got one) | 118 (4 of 20 got one; 16 of the 20 have zero comments) |

Recommendation and confidence: section 8.

## 2. What the Go SFU needs (read from the code)

`internal/sfu/sfu.go`: an answerer; `webrtc.Configuration{}` (no ICE servers); a `MediaEngine` with
PCMU (pt 0) and G.722 (pt 9) only; one send track and one receive track; the answer waits for ICE
gathering to complete (non-trickle answer), while the browser's candidates arrive later through
`webrtc_ice`; `OnConnectionStateChange` drives `Connected` once and `Failed`/`Closed` into
`fireDisconnected` (a transient `Disconnected` is deliberately not a teardown); 8 kHz G.711 is
converted to and from the 16 kHz L16 frame of the SIP side; the receive path feeds `uplinkBuffer`
(the shared jitter buffer, already ported in `src/sip/`). `internal/call/sequencer.go` waits for
`OnConnected` before answering the device. The spike keeps PCMU only (the brief) and makes trickle go
both ways, which is more than the Go code does.

## 3. S1 interop (host processes, clean runs)

Harness: `run_interop.py`. "Loss" counts sequence-number gaps at the receiver. Jitter is the RFC 3550
estimator, in ms, computed with the arrival time the library reports (str0m: `network_time`, the socket
read time; webrtc-rs: the time the application task handled the packet; pion: when `ReadRTP` returned).
Tone: Goertzel at 440 Hz over 1 s blocks of the decoded mu-law; ratio 1.0 is a pure tone.

| 60 s, 1 session | str0m | webrtc-rs |
|---|---|---|
| Rust side receives (from pion) | 2988 pkts, 0 lost, jitter mean/max 0.36/0.86 ms, max inter-arrival 24.8 ms | 2990 pkts, 0 lost, jitter 0.28/0.66, max gap 22.7 ms |
| pion receives (from Rust) | 2988 pkts, 0 lost, jitter 1.14/4.47, max gap 40.1 ms | 2989 pkts, 0 lost, jitter 0.22/1.05, max gap 28.0 ms |
| Tone detected | yes / yes (ratio 1.00 / 1.00) | yes / yes (1.00 / 1.00) |
| Sequence or timestamp discontinuities | 0 / 0 | 0 / 0 |
| Answerer RSS (5 samples) | 5.76 to 5.80 MB | 6.56 MB |
| Answerer CPU | 0.87 % of one core | 1.22 % of one core |
| Threads | 3 | 5 |
| Exit codes (answerer, peer) | 0, 0 | 0, 0 |
| Panics or error lines | none | none |
| States, answerer | ice Checking, Completed, pc Connected (0.2 s after the offer) | pc connecting, ice checking, ice connected, pc connected (0.2 s) |

The str0m jitter on the pion side (0.5 to 1.3 ms in four clean runs; 6.7 ms in a run that overlapped
a `cargo tree`, and 6.9 ms in the 50 s freeze run, cause not investigated) most likely comes from my
pacing loop (a thread with a 10 ms wake-up granularity), not from the library; the webrtc-rs tone is
paced by a tokio interval and reads 0.2 to 0.4 ms. Both are far inside what a 40 ms jitter buffer absorbs. An earlier
60 s run of the same binary while `cargo tree` was running read jitter 6.7 ms (loaded host), so treat
these as "idle host" numbers.

### 3.1 The 30-minute runs (1800 s, 1 session, idle host, one library at a time, `--sample 10`)

| 1800 s | str0m | webrtc-rs |
|---|---|---|
| Result | PASS, exit codes 0 and 0, no panic, no error line | PASS, exit codes 0 and 0, no panic, no error line |
| Rust side receives | 89,988 pkts, 0 lost, 0 sequence or timestamp discontinuities, 0 gaps over 60 ms, max inter-arrival 28.1 ms, jitter mean/max 0.38/1.41 ms | 89,990 pkts, 0 lost, 0 discontinuities, 0 gaps, max 43.4 ms, jitter 0.26/2.90 ms |
| pion receives | 89,988 pkts, 0 lost, 0 gaps, max 42.5 ms, jitter 0.42/3.04 ms | 89,989 pkts, 0 lost, 0 gaps, max 42.2 ms, jitter 0.29/2.65 ms |
| Tone, 1 s blocks | 1797 of 1797 detected both ways, ratio min 1.00 | 1797 of 1797 detected both ways, ratio min 1.00 |
| Answerer RSS (179 samples) | min 5,764 / max 5,808 / final 5,804 KB; flat, +-4 KB noise | min 6,296 / max 6,808 / final 6,808 KB: two steps, +288 KB at 650 s and +224 KB at 1560 s, flat in between |
| Answerer CPU | 0.87 % of one core (15.6 CPU-s in 1790 s) | 1.15 % of one core (20.7 CPU-s in 1790 s) |
| Threads | 3 | 5 |
| Drift (first snapshot at 10 s vs last at 1800 s) | gaps 0 and 0, lost 0 and 0, jitter max 0.49 to 1.41 ms (a running maximum; the spikes were not charted) | gaps 0 and 0, lost 0 and 0, jitter max 0.43 to 2.90 ms |
| Connection states | ice Checking, Completed, pc Connected once; nothing else for 30 minutes | pc connecting, ice checking, ice connected, pc connected once; nothing else |

No drift in packet counts, gaps or timestamps for either library (every packet, 0 discontinuities). The
webrtc-rs RSS grows by 512 KB (8 %) in two steps; a 30-minute window cannot tell an allocator arena step
from a slow leak, a multi-hour run would. The raw logs (one SNAP line per 10 s per side) are under
`/home/agent/.claude/jobs/adec9d89/tmp/runs/` and are not committed.


## 4. S1 restart, disconnect, two sessions

All on 127.0.0.1, 60 s runs unless noted; the pion peer is the offerer and requests the restart (a new
offer with `ICERestart: true`, new trickled candidates).

**ICE restart mid-call, twice (at 15 s and 35 s):**

| | str0m | webrtc-rs |
|---|---|---|
| Audio kept flowing | yes | yes, with a gap |
| Time from restart offer to ICE connected again | 10 ms, both times | 1.0 s, both times |
| Packets lost | pion rx 2 (1 per restart), answerer rx 0 | pion rx 102, answerer rx 100 (about 50 per restart per direction) |
| Max inter-arrival gap | 41 ms | 1040 ms |
| Tone detected after | yes (ratio 0.997 / 1.00) | yes (0.991 / 1.00) |
| State sequence on the answerer | Checking, Completed (twice) | pc connecting, ice checking, ice connected, pc connected (twice) |

Why webrtc-rs pauses is **not verified**; its CHANGELOG describes an "ICE-restart rebind" (new sockets
are bound on a restart), which would fit a re-check from scratch. A real restart in Portico would be a
peer changing network (Wi-Fi to cellular), where a 1 s gap is of no consequence next to the handover.

**Peer process suspended with SIGSTOP (exact PID) at 20 s for 12 s, then SIGCONT:**

| | str0m | webrtc-rs |
|---|---|---|
| Answerer state changes | none (no `Disconnected` within 12 s) | pc/ice `disconnected` 4 to 5 s after the freeze, `connected` again at the resume |
| Answerer kept sending during the freeze | yes (2988 sent) | yes (2990 sent) |
| After SIGCONT | pion saw disconnected then connected, audio resumed | audio resumed |
| Packets lost / longest gap | pion rx 435 lost (freeze), 12.03 s gap | pion rx 438 lost, 12.03 s gap |
| Tone detected | yes | yes |
| Hung or panicked | no | no |

**Peer frozen for 50 s (never comes back in time; the pion side gives up with `failed`):**

| | str0m | webrtc-rs |
|---|---|---|
| Answerer states | ice Checking, Completed, pc Connected, then one `Disconnected` at +22 s; nothing after | pc/ice `disconnected` at +4.3 s, `failed` at +29 s |
| Has a `Failed` or `Closed` state to drive teardown | no (`IceConnectionState` is New, Checking, Connected, Completed, Disconnected); the application must make `Disconnected` terminal itself, or time out | yes (`RTCPeerConnectionState::Failed`), like pion |
| Kept sending after the failure | yes, until told to stop | yes, until told to stop |

For Portico's `OnDisconnected` (teardown on Failed or Closed only, a transient Disconnected is not a
teardown): webrtc-rs maps one to one; str0m needs an application rule ("Disconnected for N seconds
means gone") because its single `Disconnected` is the only signal. str0m's `Disconnected` after about
22 s is in the same range as pion's roughly 30 s (5 s disconnected plus 25 s failed). I did not test
whether str0m returns to `Connected` from a 22 s `Disconnected` (the pion peer had already failed
by then); its source comment says it may.

**Two simultaneous sessions in one process (60 s, two pion peers started 0.5 s apart):** both libraries
pass, both directions, both sessions, 0 lost, tone detected, no cross-talk. str0m: nothing blocks it
(each `Rtc` is an independent value; I used one thread and one UDP socket per session; a tokio design
can share sockets, `Rtc::accepts` routes datagrams); RSS 6.06 to 6.14 MB, 5 threads, 1.42 % CPU.
webrtc-rs: one tokio runtime, one `PeerConnection` each; RSS 7.19 MB, 5 threads, 1.90 % CPU.

**Codec negotiation quirk:** with the offerer's rtpmap written `PCMU/8000/1` (what the repo's own
`e2e/gatesip/peer` writes) str0m 0.25 answers `m=audio 0` and `BUNDLE` empty: the m-line is rejected
and no audio flows (ICE and DTLS still complete). A browser or libwebrtc offers `PCMU/8000`, which
both libraries accept; webrtc-rs and pion accept both spellings. Not a blocker, but the e2e rig would
have to write `PCMU/8000` to run against a str0m build.

## 5. S2 static builds

Commands (also in `build_musl.sh`, run as `docker run --rm` on `rust:1-bookworm`): `apt-get install
musl-tools gcc-aarch64-linux-gnu`, `rustup target add x86_64-unknown-linux-musl
aarch64-unknown-linux-musl`, then for x86_64 `CC_x86_64_unknown_linux_musl=musl-gcc cargo build
--release --target x86_64-unknown-linux-musl`; for aarch64 `CC_aarch64_unknown_linux_musl=aarch64-linux-gnu-gcc
AR_aarch64_unknown_linux_musl=aarch64-linux-gnu-ar
CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_LINKER=aarch64-linux-gnu-gcc cargo build --release --target
aarch64-unknown-linux-musl`. The simplest approach worked first time for all four configurations, no
zig and no cmake. Release profile: `opt-level 3`, `lto = true`, `strip = true`. Build time per target
on this host: 2 to 2.5 minutes each.

| Binary (stripped) | x86_64 musl | aarch64 musl | Backend and C needs |
|---|---|---|---|
| str0m 0.25.0, default (`aws-lc-rs`) | 6,456,648 B (static-pie, ELF verified; runs on the host) | 4,789,056 B (static ELF aarch64, links; not run, no qemu-user) | DTLS: in-house `dimpl` 0.9. Primitives: `aws-lc-rs` 1.19 / `aws-lc-sys` 0.47 (C and asm via `cc`). No OpenSSL, no cmake. |
| str0m, `rust-crypto` feature | 6,514,024 B | 4,789,040 B | RustCrypto primitives (aes, sha2, hmac, p256) **but `dimpl` still depends on `rcgen` with its `aws_lc_rs` feature (certificate generation), so `aws-lc-sys` stays in the graph and in the binary**; the feature does not give a C-free build in 0.25 |
| webrtc 0.21.1, default (`crypto-ring`) | 5,387,400 B (runs on the host) | 4,657,760 B (links; not run) | DTLS, SRTP, ICE, STUN in-house (`rtc-*` crates). Primitives: `ring` 0.17.14 (C and asm via `cc`). `rustls` 0.23 and `x509-parser` are also in the graph (TURN over TLS). No OpenSSL, no cmake. |
| webrtc 0.21.1, `crypto-aws-lc-rs` | 7,460,200 B (a 20 s interop run with it passed) | 5,903,176 B (links; not run) | `aws-lc-rs` only (verified with `cargo tree`: `ring` absent) |

Dependency count (`cargo tree --prefix none -e normal`, unique crates including the answerer itself, same
for both musl targets within 1): str0m 86 (94 with build dependencies); webrtc 179 (187). The webrtc
number includes tokio, which Portico already has; str0m has no async runtime in it.

Resident memory (answerer, from `/proc`): idle, listening, no session: webrtc-rs 3.5 MB (tokio runtime,
5 threads); str0m reads 4 kB for the musl binary (`statm` shows one resident page; I cannot explain
it, treat it as a /proc accounting oddity) and 1.5 MB for the glibc build I tried first. In a call: str0m
5.8 MB, webrtc-rs 6.6 MB (one session), 6.1 and 7.2 MB (two sessions). The Go figure the plan guesses
(20 to 40 MB in a call) was **not measured here**.

## 6. S3 ffmpeg from a tokio program

`s3-ffmpeg/`: spawns `ffmpeg` with `tokio::process::Command`, writes 100 frames of 20 ms of 16 kHz
s16le PCM (a 440 Hz tone) to its stdin at real-time cadence, closes stdin, reads 8 kHz s16le from stdout.
Result: `ffmpeg exit=0`, 32000 bytes = 16000 samples at 8 kHz (exactly the expected 2.0 s), 440 Hz energy
ratio 1.000, 2.15 s wall. ffmpeg is not on the host and not in `rust:1-bookworm`, so the script
`apt-get install`s it inside the throwaway container (Debian ffmpeg 5.1.9); the Go driver's real command
line (libopus to RTP) was not exercised. No problem found: spawn, piped stdin and stdout, EOF as the stop
signal and `kill_on_drop` are all in tokio's `process` feature.

## 7. Liveness (GitHub API without a token, crates.io, RustSec; `liveness.py`, 2026-11-12)

| | str0m | webrtc-rs (`webrtc`, async layer) | webrtc-rs (`rtc`, sans-IO core) |
|---|---|---|---|
| Releases in the last 12 months | 22 stable on crates.io (0.12.0 on Nov 21 2025 to 0.25.0 on Nov 3 2026; GitHub Releases are not used) | 12 stable (0.17.x and 0.20.x/0.21.x) plus 15 pre-releases (alpha, beta, rc) | 28 stable (0.3 to 0.9, 0.20.x, 0.21.x), 43 including pre-releases |
| Commits per month, last 6 months | May 11, Jun 22, Jul 13, Aug 15, Sep 21, Oct 50, Nov (12 days) 7: 139 | Jun 42, Jul 56, Aug 60, Sep 18, Oct 10, Nov (12 days) 3: 189 | Jun 15, Jul 79, Aug 162, Sep 27, Oct 17, Nov 17: 317 |
| Distinct commit authors, last 6 months | 43 (top: algesten 57, xnorpx 13, Jaltaire 5, mstyura 5) | 14 (rainliu 162 of 189 = 86 %) | 43 (rainliu 213 of 317 = 67 %, StefanoD 23) |
| Open issues plus PRs now | 44 | 16 | 25 |
| Median hours to first maintainer reply, 20 most recent issues | 21.4 (9 of 13 had one; 3 of the 4 without were filed by the author's own accounts, algesten and lolgesten (second account assumed)) | 118 (4 of 20 had one; 16 of 20 have zero comments: 12 closed silently, 4 still open for 11 to 37 days) | 13 (2 of 20; 18 of 20 have zero comments, 3 of them filed by the maintainer himself) |
| Published GitHub security advisories | 0 | 0 | 0 |
| RustSec advisories (crate dirs in advisory-db) | none for str0m, dimpl, sctp-proto | none for webrtc, webrtc-util, -dtls, -srtp, -sctp, -ice | none for rtc, rtc-dtls, -srtp, -ice, -stun, -rtp (but `ring` and `aws-lc-sys` have advisories, both are dependencies) |
| Stars | 654 | 5184 | 168 |
| Downloads, last 90 days (crates.io `recent_downloads`) | 1.35 M | 3.73 M | 1.83 M |

Caveat on the response column: it measures comments on issues, not whether a fix landed; a maintainer
who fixes an issue in a commit and closes it without comment counts as "no reply". The sample is small
(13 and 20 issues, each repo's latest) and 2 to 3 weeks wide. str0m also routes questions to Discord
(its README says so), which this does not see.

Notes: the webrtc-rs async crate's own commit rate fell from 60 to 3 per month since August while its
sans-IO core `rtc` carries the work; the 0.17 line (the old design) is still getting patch releases
(0.17.3 on Nov 7). webrtc-rs ties its 1.0 to "a stable public API" and says a 0.x minor can break;
str0m's 0.x minors carry breaking items every time (below).

**What the last two minors would have cost me.** I compiled the same answerer source against the
previous minors (scratch copies, `cargo check`):

- str0m 0.24.1 to 0.25.0: **zero source changes**. CHANGELOG 0.25.0 lists breaking items (BWE
  `set_bitrate(min, desired)`, MSRV 1.89, dimpl 0.9, no SDP `merge()`, VP9 picture ids, fixed video
  reordering timeout removed); none touches an audio-only PCMU answerer.
- str0m 0.23.1 to 0.24.0: **one line**: `enable_pcmu(enabled)` gained a second argument (`use_red`), from
  "RFC 2198 RED for all audio codecs (breaking)".
- webrtc 0.20.5 to 0.21.1: **zero source changes** to compile. The CHANGELOG 0.21 items are additive
  (crypto-provider features, `crypto` re-export) plus two behaviour changes that do not show in
  types: `with_udp_addrs` re-resolves the addresses on every bind and a wildcard now means "one socket
  per interface, loopback skipped", and an address that cannot be bound is skipped, not fatal. I did
  not run the 0.20.5 binary.
- Compiling is not behaving: str0m's churn has been behavioural (pacing, NACK, BWE, SDP strictness), the
  same for webrtc-rs. I cannot see that from a type check.

**ICE-lite, TCP, TURN (not needed by Portico).** str0m: ICE-lite supported (`set_ice_lite`), UDP and TCP
candidates (IPv4 and IPv6) supported, **no TURN client and no STUN-based gathering**: it is sans-IO and
treats NIC enumeration, STUN and TURN as sockets the application obtains and adds as candidates
(README "NIC enumeration and TURN"). webrtc-rs: has `rtc-turn`, STUN, ICE-TCP examples in the repo, and
binds and gathers host candidates itself.

## 8. What the production port would look like

The Go surface to replace is small: `internal/sfu` (543 lines + `uplink.go` 111, `g711.go` 87, `g722.go`
151, `resample.go` 66) and `internal/call` (465), 1.9k lines of Go tests, plus the three IPC handlers in
`main.go` (`webrtc_offer`, `webrtc_answer`, `webrtc_ice`) and the Python-visible behaviour. The jitter
buffer (`pkg/sip/jitter.go`) is already in Rust (`src/sip/`).

### 8.1 str0m

- Shape: one `Session` struct owning an `Rtc`, driven by one tokio task per call: `loop { drain
  poll_output (Transmit goes to the socket, Event to the session); select! { udp recv, sleep_until(the
  Timeout), command channel (AudioIn frame, add candidate, offer, close) } }`. The spike used blocking
  sockets and a 10 ms wake-up; a production loop uses `tokio::net::UdpSocket` and the returned
  `Instant`.
- Who owns what: the application owns the sockets, the clock, the candidates and the timers.
  Interface enumeration (LAN and tailnet host candidates: one socket and one `Candidate::host` per
  address; `Receive.destination` has to be a local candidate address, which a wildcard socket does not give you without IP_PKTINFO; not tested) is about 50
  lines plus a crate such as `if-addrs`; the Go side got it from pion for free. No mDNS, no STUN, which
  matches Portico.
- Jitter buffer: str0m has a fixed reordering buffer for audio (`set_reordering_size_audio`) and no
  adaptive buffer; Portico's own `uplinkBuffer` stays as it is, fed from `MediaData` (frame mode, one
  PCMU packet per event) or from raw RTP (`set_rtp_mode`). Outgoing: the application passes
  `Writer::write(pt, wallclock, rtp_time, payload)`; str0m owns sequence numbers and SSRC.
- Teardown: map `IceConnectionState::Disconnected` to "start a grace timer", `Event::Connected` once to
  `OnConnected`; there is no `Closed`/`Failed` event, `Rtc::disconnect()` is the close.
- Porting list: SDP/answer (`accept_offer`), trickle in (`add_remote_candidate`), our candidates out (we
  construct them), `g711`/`resample`/`uplink` (mechanical), G.722 (needs a Rust codec: **no crate chosen
  or tested**), tests (`internal/sfu` and `internal/call` Go tests have no WebRTC-library dependence
  beyond `sfu_test.go`, `tone_test.go`, `wideband_test.go`, which would be rewritten against a Rust
  loopback pair).
- Effort (estimate, **not measured**): 3 to 4 builder-sessions for `Session`, audio conversion, the
  three handlers and the tests; 1 more for browser and Android-app interop and a real long call.
- Top three risks: (1) churn: five minors in the 18 weeks from June 27 to November 3, most with at
  least one breaking item, and a long tail of behavioural changes in a fast project; 22 releases a year means a pin, a dated upgrade
  routine and a re-run of this harness per bump; (2) the application owns ICE housekeeping that pion did
  (interface enumeration, the Disconnected-means-gone rule, an application-level liveness check): the
  code is small but it is Portico's to get wrong; (3) strictness in SDP matching (`PCMU/8000/1` rejected)
  against clients not yet tested: the Android app and Safari are **not verified**.

### 8.2 webrtc-rs

- Shape: `PeerConnectionBuilder` + an `Arc<dyn PeerConnectionEventHandler>` whose `on_track`,
  `on_ice_candidate`, `on_connection_state_change` replace pion's callbacks one to one; `TrackLocalStaticRTP::write_rtp`
  and `TrackRemote::poll()` replace `WriteRTP`/`ReadRTP`. The `Session` API (`HandleOffer`,
  `AddICECandidate`, `OnConnected`, `OnDisconnected`, `In`/`Out` channels) ports almost line for line;
  `webrtc_ice` becomes `add_ice_candidate`.
- Who owns what: the library's driver task owns the sockets, timers, DTLS and ICE; `with_udp_addrs`
  takes the bind list (a wildcard binds one socket per non-loopback interface and gathers host
  candidates itself, matching pion's behaviour; the spike bound 127.0.0.1 only because the test is
  loopback). Tokio is a hard dependency here (Portico's Rust crate already uses it).
- Jitter buffer: none in the library; Portico's own stays (raw `rtp::Packet`s arrive on `poll()`).
- Awkward: `rtc` is a private dependency, so the application adds `rtc =0.21.1` itself for the codec
  and track types (`MediaEngine`, `RTCRtpCodec`, `MediaStreamTrack`, `rtp::Packet`); two version pins
  that must match. Interceptors (NACK, RTCP reports, TWCC) come from `register_default_interceptors`.
  `Failed`/`Closed` exist and match pion.
- Effort (estimate, **not measured**): 2 to 3 builder-sessions, then the same 1 for interop and a long
  call.
- Top three risks: (1) maintenance concentration: 86 % of the async crate's commits and two thirds of
  `rtc`'s are one person, issues mostly unanswered, the async crate's own commit rate has dropped to a
  few a month; (2) the 0.20/0.21 line is about three months old (it calls itself the run-up to a stable
  API), 14 releases including pre-releases between July 31 and October 11, and the old 0.17 line is still patched; (3)
  the ICE-restart pause of about 1 s and a larger dependency surface (179 crates, `ring`, `rustls`,
  `x509-parser`) for a use that needs a small part of it.

### 8.3 The hybrid (a small Go helper keeps pion for the SFU only)

Not built here. It keeps the library that has production mileage and the Go tests of `internal/sfu`, at
the price of a second binary or a subprocess per HA install, an audio hop (PCM over a local socket or
stdio) per direction, a second language in the wheel, and the cross-build of a Go program for the same
targets (pion builds `CGO_ENABLED=0`, so that part is easy: `peer-go` is 8.1 MB static). It defeats
the "one language, memory safety" goals for exactly the part the Rust crates are least proven in.
**Not verified**: the added latency, memory (a second Go runtime at about 6 MB idle was measured for
the Go transport in the plan) and the wheel packaging.

## 9. Recommendation

**str0m, with moderate confidence (about 60 %); webrtc-rs about 30 %; the hybrid about 10 %.** This
spike can only rule options out, and it ruled none out: both libraries interoperate with a pion offerer
on loopback, both ran 30 minutes with zero lost packets and no panic, both build static for x86_64 and
aarch64 with the plain `rustup target add` + gcc route. So the choice rests on fit and project health,
and on one thing nobody has tested (real clients).

Reasons for str0m:
- Project health is the better of the two on every number I could get: 43 distinct authors in six months
  against 14 on the webrtc-rs async crate, a maintainer reply to 9 of the latest 13 issues in a median of
  21 hours against 4 of 20 in 118 hours, 22 releases a year each with a CHANGELOG that names breaking items.
- The behaviour matches Portico's needs more closely: an ICE restart that does not interrupt audio (10 ms,
  at most one packet), a smaller dependency graph (86 crates against 179, no async runtime inside), the
  lower memory and CPU, and a design made for servers that own their own sockets and clocks. The
  last two minors cost me zero lines (0.24 to 0.25) and one line (0.23 to 0.24) for this use.
- The extra work it asks for is known and small: candidate gathering for the LAN and tailnet interfaces,
  an application rule that turns `Disconnected` into teardown, and keeping the `rtpmap` spelling in mind.

Reasons against, which are real: str0m has no `Failed`/`Closed` state and no TURN/STUN (not needed), it
rejects `PCMU/8000/1`, its minors break something about every month, and `rust-crypto` is not C-free in
0.25. webrtc-rs maps onto the Go `Session` almost one to one (1 to 2 fewer builder-sessions, my
estimate), has `Failed`, and gathers interfaces itself; against it are the 1 s restart pause, the thin
maintenance (one person writes 86 % of the async crate, issues mostly unanswered, the async crate's own
commit rate falling), and a young line.

What would change my mind:
- A browser (Chrome, Safari, Firefox) or the Android app fails against str0m for an SDP or ICE reason
  that webrtc-rs handles: then webrtc-rs. Both need that test before G1 closes; this harness has the
  signalling protocol for it only on loopback.
- str0m staying in `Disconnected` after the peer comes back (not tested for long outages), or the
  application-level liveness rule growing beyond about 100 lines: then webrtc-rs.
- Either library showing a failure on a real LAN/tailnet path with several interfaces, or a multi-hour
  run with real RSS growth (webrtc-rs stepped +8 % in 30 minutes): then the other, or the hybrid.
- The hybrid wins if neither survives real-client interop, or if keeping up with a breaking minor every
  four to six weeks looks like more upkeep than the pion Go helper's one process boundary.

The kill criterion of the plan ("S1 cannot interoperate after reasonable effort and the hybrid is
rejected, or S2 cannot build a target the maintainer needs") is not met on the evidence here.

## 10. What I did not verify

- Any real browser or the Android app: the offerer is pion on loopback, so no libwebrtc SDP, no NetEq,
  no mDNS candidates, no real LAN or tailnet path, no NAT, no multi-interface candidate sets. The plan's
  S1 asks for those; this run cannot stand in for them.
- aarch64 execution (no qemu-user on the host); only that the static ELF links.
- Behaviour above 2 sessions, under CPU load, with packet loss or reordering (no network emulator), and
  with G.722.
- Whether str0m recovers from a long `Disconnected`; why webrtc-rs pauses 1 s on a restart.
- The Go figures in the plan's cost table (no Go-side RSS or CPU was measured here).
- Effort numbers are estimates from the size of the Go code, not from a port.
- Issue-response medians use a few comments per repo; "no reply" can mean "fixed by commit".

## Review addendum (independent review, 2026-11-12) and the parent's reading

An independent read-only review checked this report against the code, the raw run JSON/logs, the str0m 0.25.0 source, crates.io and the GitHub API. The numbers in the 30-minute runs, the restart/SIGSTOP/peer-gone sequences and the liveness counts trace to logs and match. Caveats it found, all accepted:

- **Not like for like.** str0m's meter sees `MediaData` after its own reordering/depacketising; webrtc-rs's meter sees raw RTP. Receive timestamps are taken at different points (blocking recv vs tokio task), so the jitter figures are not comparable, and the pacing loops differ (str0m wakes every 10 ms with catch-up; webrtc-rs uses a tokio interval with Skip). The str0m loop also reads `std::env::var("DBG")` several times per iteration (a small CPU handicap for str0m). "0 lost" counts sequence gaps only, equal for both.
- **Invalid row.** The str0m musl "idle RSS 4 kB, 1 thread" is not physically plausible: ignore it. The in-call RSS rows stand.
- **`PCMU/8000/1` rejection is a real str0m limitation** (`payload_params.rs::match_score`: a channel count of `Some(1)` differs from `None`). libwebrtc (Chrome, Safari, Android) and Firefox write `PCMU/8000`, so browsers should be fine; the repo's own e2e peer and any non-browser peer may send `/1`. Mitigation: normalise `/1` out of the offer before `accept_offer`, or patch str0m upstream; not "change the e2e rig".
- **Remote SDP can panic str0m** (issue #1111, fixed): run `accept_offer` under `catch_unwind` or in an isolated task.
- **Dead-peer detection.** str0m has no `Failed` state, but the Go `house.Bridge` already has an uplink-silence watchdog (`DefaultSilence` 10 s) that ends a call whose peer vanished regardless of ICE state, and str0m's 22 s detection is tunable with `RtcConfig::set_max_stun_retransmits`. webrtc-rs maps `Failed`/`Closed` one to one onto the Go teardown. For str0m the port must add a `fireDisconnected` equivalent (Disconnected plus a grace timer, or the media-silence check) and own candidate enumeration and host candidates in a non-trickle answer (the Go code answers non-trickle; the spike only exercised trickle).
- **Codec: PCMU only, by design.** The Go SFU registers PCMU alone (`sfu.go` newSession comment: with the default set a phone's offer lists Opus first and the hub once decoded Opus as mu-law). The Rust answerer must enable PCMU alone and still pick it when the offer lists Opus/G.722 first. G.722 is NOT needed. (A reviewer remark that the SFU prefers G.722 was checked against the code and is wrong.)
- **Liveness samples are small** (7 to 20 issues per repo); a webrtc-rs issue with zero comments is often "closed, completed", so "unanswered" overstates the neglect.
- **Untested and now required before G1 is final:** a real libwebrtc/Firefox offer (bundle, rtcp-mux, extmap, ssrc-group, fmtp, Opus first with PCMU also offered), mDNS `.local` candidates, IPv6 and multiple interfaces, trickle arriving before the answer is sent, re-offers, DTLS role, large candidate sets, `max_stun_retransmits` tuning.

**Correction (2026-11-12):** the statement above that the Go SFU is PCMU-only and that G.722 is not needed was true when written and is NOT true now. Go PR #282 (merged 2026-11-11 evening, `sfu: wideband G.722 on the phone leg, PCMU fallback`) registers G.722 and PCMU in `newSession`, so the answer names G.722 and PCMU in the offerer's order and a phone sends G.722 (16 kHz, the gate is 16 kHz). Opus is still never answered. The Rust SFU port therefore needs G.722 negotiation and a G.722 encoder/decoder; see `RESULTS_S1b.md` addendum.

**Parent's reading (G1, provisional).** str0m is the first choice (healthier project, seamless ICE restart, smaller graph), with webrtc-rs as the fallback if real-browser interop fails or str0m cannot recover from a long Disconnected; the 60/30 split is soft because webrtc-rs maps teardown one to one and is simpler to port. G1 is declared **provisionally passed for str0m** and becomes final only after the real-browser interop spike (S1b) below passes.
