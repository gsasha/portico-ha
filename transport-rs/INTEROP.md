# transport-rs: real-client interop (DEV-764 slice 4b)

Date of the runs: 2026-11-12. Harness, scenarios and pass criteria: [tests/interop/README.md](tests/interop/README.md).
Every number below was measured in this session on the sandbox host unless it says **not verified**.

**Setup.** Host: 4 cores, Debian 12, kernel 6.1. The binary under test is the REAL `portico-transport` of this
tree (release profile, glibc, built in `rust:1-bookworm`; 4.7 MB), started as a plain host process with
`-socket`, configured over its IPC with a `gate_sip_server` (one per door), no environment overrides (the
default interface allow-list, the default grace and `max_stun_retransmits`). Browsers: **Firefox 153.7.0esr**
(`/usr/bin/firefox --headless`, throwaway profile, a plain host process killed by its process group) and
**Chromium 124** (`zenika/alpine-chrome:latest`, image `eb3378c1ed00`, headless, `docker run --rm --network host`,
killed with `docker kill` of its own container name). The offerer is `tests/interop/page.html` (a WebAudio
oscillator as the microphone, so the tone is known). The door is a scripted SIP station in Python (REGISTER with
digest, INVITE offering `PCM/16000`, a tone as 20 ms RTP of 16 kHz L16, and a receiver that FFTs what the transport
sends it). The path in every scenario is **station -> transport -> browser and browser -> transport -> station**,
through the real SIP server, the real driver, the real `webrtc_offer` / `webrtc_ice` handlers and the str0m session.
Host candidates: the browser reaches the transport at its host address (`10.4.0.2` and the global IPv6 of `eth0`)
because the allow-list skips loopback and the docker bridges.

**What was run, in order.** (1) A first full matrix on a build made before the select-order change of the session loop:
every scenario passed on both browsers. (2) A second full matrix on the static musl build: 15 of 18 passed; the three failures were
all the criterion "the station hears the browser's tone in 70 % of the windows". That led to the diagnosis below
(the uplink buffer's phase sensitivity) and to a change in `sip::JitterBuffer` (PARITY.md difference 77). (3) **The final
matrix, on the final source (glibc release build of the committed tree): Firefox 9 of 9, Chromium 8 of 9** (H3 fails that same
criterion, 41 of 59 windows), plus `hostile.py` 37 of 37. The tables below are the final matrix.

## Re-run after the review fixes (second commit)

The review of the first commit led to fixes (PARITY.md differences 79 to 81: sockets bound only after the offer parsed, a refused
first offer's session dropped at once, a session cap, validated `PORTICO_SFU_*` values, a backoff for a failing UDP reader, the
panic guard on every step of the session loop, a bounded wait at shutdown). **Build used for this section: glibc release,
`portico-transport` sha256 starting `3a4cc117fe0f3284`, built from the tree of the second commit.** Scenarios A, E and F were
run again on both browsers, and `hostile.py`:

| | Firefox 153.7 | Chromium 124 |
|---|---|---|
| A (60 s G.722) | 12/12, station heard 5000 Hz in 232 of 234 windows | 12/12, 233 of 235 |
| E (browser killed) | 14/14, `Disconnected` 22.36 s, session over and station BYE 28.36 s | 14/14, 24.10 s and 30.10 s |
| F (two doors) | 21/21 | 21/21 |
| G (`hostile.py`, 37 cases, the default session cap lifted to 64 for its flood of 50 calls) | 37/37 | |

CPU 1.8 to 2.1 % (one session) and 3.2 to 3.4 % (two); RSS in call 6.6 to 7.1 MB. (The earlier A runs had 8 to 10 % of windows
away from the tone; in these two there are 2 of about 234: the figure moves with the tick phase of each run, see Observations.)

**Go on Chromium, scenario H3** (the same harness, the real Go binary, four runs, 20 ms chunks of the decoded tone below half level
inside the call): 4, 59, 143, 144. Two of four runs are in the bad regime (a 200 ms oscillation) that failed the Rust run on
Chromium, and both still pass the 70 % criterion narrowly (42 of 59 windows). So Go shows the same Chromium behaviour; the earlier
comparison against Firefox only is no longer the whole story. Still to do before production: the paced-peer stall test
(`docs/parity/webrtc_sfu.md`, "Before production (4b)").

## Results (final matrix)

Each cell is "criteria passed / checked"; a scenario passes only when every criterion does. The criteria
(`harness.py`, `check`) include: the answer names the right codecs in the right order, the answer is non-trickle
with host candidates, the browser receives AND sends the expected codec (its `getStats()`), the 200 OK to the
station is not before the browser's `connected`, the browser's FFT peak is the station's tone, the station's FFT
peak is the browser's tone (at least 70 % of the loud windows), the station is hung up when the browser ends.

| Scenario | Firefox 153.7 | Chromium 124 | What it shows |
|---|---|---|---|
| A: 60 s, Opus offered first then G.722, wideband tones (browser 5 kHz, station 6 kHz) | **12/12** | **12/12** | answer `G722/8000` then `PCMU/8000`, no Opus, no red / CN / telephone-event; the browser reports `audio/G722` in and out; the browser hears 6002 Hz in all 60 of its one-second reports and the station hears 5000 Hz in 214 of 234 (Firefox) and 211 of 235 (Chromium) 0.26 s windows (the rest: Observations). Neither tone fits in PCMU (4 kHz): G.722 is really on the wire, decoded and encoded by the Rust code. No stall above 113 ms in 60 s |
| B: the browser offers PCMU only | **12/12** | **12/12** | answer `PCMU/8000` only; PCMU both ways; 1 kHz heard at the station in 78 of 78 windows, 440 Hz in the browser |
| C: Opus first, PCMU listed before G.722 | **12/12** | **12/12** | answer `PCMU`, `G722` (the offerer's order decides, difference 61); PCMU both ways |
| D: `restartIce()` at 20 s of 45 s | **13/13** | **13/13** | the same session carries on; max stall 111 ms (Firefox) and 103 ms (Chromium) |
| E: the browser is killed 15 s into the call | **14/14** | **14/14** | see "Teardown" |
| F: two doors, two browsers at once, different tones per door | **21/21** | **21/21** | each station hears its own browser's tone (5000 / 3000 Hz) and each browser its own station's (6002 / 2003 Hz); no leak; both stations hung up |
| H1: candidates trickle right after the offer | **12/12** | **12/12** | the keyed lane delivers each candidate after its offer; connects in about 4 s (Firefox), about 1 s (Chromium) |
| H2: candidates inside the offer | **12/12** | **12/12** | |
| H3: candidates BEFORE the offer | **13/13** | **12/13** | `webrtc_ice` before `webrtc_offer` is `unknown_session` (as in Go); the call connects anyway through the browser's own checks (peer-reflexive), as in S1b. Chromium: the one failed criterion is "the station hears the browser's 1 kHz in 70 % of the windows" (41 of 59), see Observations |
| G: 37 hostile offers, candidates and answers over the IPC (`hostile.py`, no browser) | **37/37** | | the process survives every one, every refusal has Go's code, a valid offer is answered after all of it and 50 renegotiations and 50 distinct calls work |

## Teardown (scenario E): a vanished browser

The browser is SIGKILLed (Firefox: the process group; Chromium: `docker kill`), so no DTLS alert and no RTCP BYE
is sent; time zero is the kill. Defaults: `max_stun_retransmits` untouched, grace 6 s.

| | Firefox | Chromium | Go (pion, S1b) |
|---|---|---|---|
| str0m reports `Disconnected` | 22.26 s | 24.10 s | `disconnected` 5.0 / 4.7 s |
| `Disconnected` did not recover in 6 s: session closed, `OnDisconnected` fired once | **28.26 s** | **30.10 s** | `failed` 30.2 / 29.9 s |
| BYE received by the station | 28.26 s | 30.10 s | |
| The call slot is free (a new INVITE is announced with `ringing`) | yes | yes | |
| `webrtc_ice` for the dead call | `unknown_session` | `unknown_session` | |

(An earlier run of the same scenario gave 22.06 / 28.06 s and 24.08 / 30.08 s.) The end-to-end time stays at Go's ~30 s.
Reading it: str0m's dead-peer detection (21.9 to 24.2 s in S1b, the same here) cannot be shortened reliably (S1b:
`max_stun_retransmits` 3 to 5 gave 10.6 to 15.8 s, not a clean lever) and a shorter total would only trade a false
hang-up on a lossy link for speed, so the defaults were kept and the grace was sized to bring the total to Go's. With
`PORTICO_SFU_MAX_STUN_RETRANSMITS` and `PORTICO_SFU_DISCONNECT_GRACE_SECS` the numbers can be moved without a rebuild.
A browser that closes its call cleanly (scenarios A to D, F, H: `pc.close()`) ends the session at once and the station
gets its BYE within the same second (the DTLS close_notify is seen); the cargo test
`the_peer_closing_gracefully_ends_the_session_at_once` covers it without a browser.

## Memory and CPU

VmRSS of the Rust binary, read from `/proc` once a second: **1.5 MB idle** (listening, no device), **4.0 MB with a
configured `gate_sip_server`** (sockets and tasks), **6.2 to 6.9 MB in a call** (one session, 60 s), **7.1 to 7.2 MB
with two sessions** (scenario F); flat over the 60 s call. The static musl x86_64 build measured 1.4 MB idle and 6.8 MB
in a call (scenario B). CPU of the transport process over the call: **1.8 to 1.9 % of one core with one session, 3.4 to
3.5 % with two** (G.722 both ways, 50 packets a second each way). The Go binary measured 8.2 MB idle in slice 1; its in-call
figure was not measured here.

## Observations

* **The browser -> station direction loses a slot now and then, and a few runs lose many (the uplink buffer).** In the 5 kHz
  runs 8 to 10 % of the 0.26 s FFT windows (A: 20 of 234 Firefox, 24 of 235 Chromium) have a dominant frequency that is
  not the tone: a window holding a lost 20 ms slot, whose G.722 decoder takes 40 to 100 ms to settle. Cause, from traces
  and a replay (`docs/parity/webrtc_sfu.md`, "Go oddities found", slice 4b): these headless browsers send 50.6 to 50.7 packets a second
  against the buffer's 50, the Go `JitterBuffer` (ported unchanged) fills to its 9-frame window and refuses the newest
  packets, and the loss depends strongly on the tick phase (13 to 172 silent slots of 760 for the SAME packets). The real Go
  buffer, fed the same recorded arrivals, loses 9 to 24; the Rust one on exact time 13 to 18; live with the same Firefox
  and harness Go and Rust now give the same distribution (6 runs each: Go median 34, Rust median 37 degraded 20 ms chunks
  out of about 750). A paced str0m peer sending the same tone for 20 s gives 0 bad windows of 70
  (`uplink_wideband_soak_has_no_glitch_windows`). **The clock skew is probably this VM's; real handsets are 50 to 200 ppm
  (not verified), 60 to 250 times less.** The one Chromium H3 failure is the bad tail of this: from 10.6 s of that call the buffer
  oscillated with a 200 ms period (4 to 5 silent chunks, then 5 of tone).
* The 200 OK to the station was sent 0.00 to 0.03 s BEFORE the page reported `connectionState connected` in some runs
  (the transport is connected when its DTLS completes; the browser's event follows). The harness allows 0.3 s; the real
  rule (nothing is answered before the transport side connected) is proved by
  `server_ring_never_answered_before_the_browser_connects` and by the 300 ms window in
  `station_to_browser_both_ways_through_a_real_session`.
* After the browser is killed the uplink jitter buffer keeps playing its silence-filled slots to the station until the
  session ends (a DC level of rms about 200 in the last windows): the 4a behaviour, as in Go.
* Headless Firefox sends the occasional RTP packet with a repeated timestamp and the next sequence number (6 in 15 s in
  the trace of one run); the buffer is keyed by sequence number and plays them.

## Not covered

Safari, the Android app and a phone away from home; packet loss and jitter on a real network (loopback and the local
interface only); a recovery of `Disconnected` to `Connected` inside the grace with a real browser; more than two
simultaneous sessions; calls longer than 60 s; the aarch64 binary (links, not run).

## Re-run on post-round-3 main (R3A-2), 2026-11-17

Source: `origin/main` a49c055 (includes the round 2/3 fixes: SFU source check, datagram batching). Binary: glibc release,
`cargo build --release` in `rust:1-bookworm`, sha256 `058d96a634db8ed620a6e236219eafd42e2d16d41056405a36203e0945e6a301`.
Browsers: Firefox 153.7.0esr (host, headless, throwaway profile), Chromium 124 (`zenika/alpine-chrome`, image `eb3378c1ed00`, `--network host`).
Same host, harness and criteria as above; the source check is on (the default).

| Scenario | Firefox 153.7 | Chromium 124 |
|---|---|---|
| A | 12/12 | 12/12 |
| B | 12/12 | 12/12 |
| C | 12/12 | 12/12 |
| D (`restartIce`, roaming) | 13/13 | 13/13 |
| E | 14/14 | 14/14 |
| F | 21/21 | 21/21 |
| H1 | 12/12 | 12/12 |
| H2 | 12/12 | 12/12 |
| H3 | 13/13 | 12/13 (the known 70 % criterion, 41 of 59 windows, identical to 2026-11-12) |
| G (`hostile.py`) | 37/37 | |

Firefox 9/9, Chromium 8/9: the same result as the 2026-11-12 table.

`udp_refused`: the shipped binary does not expose it (no log line, no IPC field). For this measurement only, a throwaway build
(sha256 `7f8c9b57...`, one extra pair of fields in the closing `uplink:` log line, NOT committed) ran A and D on both browsers:
`udp_refused=0 input_errors=0` in all four calls (A 3034 / 3054 RTP packets, D 2275 / 2292), so the source check refused nothing from
a legitimate browser, including after `restartIce`. The hostile side (`udp_refused` rises for a foreign sender) is covered only by
`tests/sfu_flood.rs`, not by a browser run. Not done here: PARITY 441 (release build, two addresses, a real LAN, a phone that really
changes network).

## PARITY 441, release build under a datagram flood (DEV-764), 2026-11-17

Source: `origin/main` f24b233. Binaries: glibc release, `cargo build --release` in `rust:1-bookworm`, private target dir. Clean build
sha256 `bec5724d8517a66294ff573308aeb004c9f822f695c4c592e2cf995804da1628`. Throwaway logging build (the closing `uplink:` log line
gains `udp_refused` and `input_errors`, NOT committed) sha256 `a23e0968c0905187bd320c6a13c345c0a8fc86c1fdf1a34575d48233cc6e40c3`.

**Criterion, stated before the runs:** device audio (the station's tone) still reaches the peer (the browser) in at least 70 % of
windows during the flood, and the session is not dropped. The other direction (the browser's tone to the station) was measured too.

**Setup.** Scenario A (60 s call, headless Firefox 153, browser 5 kHz, station 6 kHz, G.722), the unmodified `tests/interop/harness.py`
logic (a /tmp copy that also dumps the per-packet arrival times and page reports). The transport ran pinned to CPUs 0-1 (`taskset`).
From 15 s after the transport's session sockets appeared, a throwaway Python flooder (4 processes, 2 per address, unpinned, `nice 10`
with the run) sent garbage for 25 s to the session's own UDP sockets, one per address: this host's eth0 IPv4 (10.4.0.2) and global
IPv6 (2a0d:6fc0:988:106:...), each sender bound to that same address (so source = destination host, a foreign port). Payloads rotated
over RTP-looking (0x80...), DTLS-looking (0x17 fe fd) and a STUN binding request without integrity. 640k to 660k datagrams/s were sent
in total (16.1 to 16.5 M in 25 s). Windows: station = 4096-sample FFT windows (0.256 s) with the dominant tone within 60 Hz and rms
above 300; browser = the page's 1 s reports with `domHz` within 60 Hz of the station tone. The first second of the flood is left out.

| Run (binary) | Station hears browser tone, pre | during flood | Browser hears station tone, during flood | Transport CPU in flood (1 core = 100) | RSS during flood | kernel UDP drops in flood | `udp_refused` | rtp_rx from browser (whole call) | session |
|---|---|---|---|---|---|---|---|---|---|
| baseline 1 (logging) | 43/47 (91 %) | 89/94 (95 %) | 24/24 | 2.1 | 7.9-8.5 MB | 0 | 0 | 3035 | survives, harness verdict 12/12 |
| baseline 2 (clean) | 46/47 (98 %) | 92/94 (98 %) | 24/24 | 2.0 | 7.8-8.3 MB | 0 | n/a | 3036 | 12/12 |
| flood 1 (logging) | 44/47 (94 %) | 68/94 (72 %) | 25/25 | 50.2 | 8.4 MB | 8.05 M | 5.49 M | 2697 | 12/12 |
| flood 2 (logging) | 47/47 (100 %) | 69/94 (73 %) | 24/24 | 46.2 | 8.0-8.5 MB | 8.34 M | 5.05 M | 2587 | 12/12 |
| flood 3 (clean) | 46/47 (98 %) | 79/94 (84 %) | 24/24 | 52.7 | 8.5 MB | 7.26 M | n/a | 2526 | 12/12 |

Idle-in-call CPU was 1.9 to 2.4 % before and after the flood. `audio_out_frames` (device frames sent to the peer) was 3000 of 3000 in
all three flood runs, `udp_dropped=0`, `udp_tx_dropped=0`, `input_errors=0`, the browser's inbound G.722 stream showed 0 packets lost
and no concealment added during the flood. The browser-to-station direction lost 340 to 510 of 3035 RTP packets over the whole call
(11 to 17 %, all of it in the 25 s flood), jitter-buffer `silence_slots` 599 to 708 against 254 to 435 unflooded.

**Verdict: PASS.** Device audio reached the peer in 100 % of the windows during the flood (3 of 3 runs), the session never dropped, no
process died, RSS did not grow (+0.5 MB). The reverse direction (what the phone says) stayed above the 70 % line in all three runs
but narrowly (72 %, 73 %, 84 % against 91 to 98 % unflooded).

**Where the reverse loss comes from (learned, partly a guess).** About half the flood was dropped by the kernel on the socket's receive
queue (7.3 to 8.3 M drops); the browser's genuine RTP shares that queue, so a fraction of it is dropped before the process sees it.
That is not something the application loop can prevent; the loop did what the fix claims (it refused 5 M datagrams in 25 s, about
200k/s, on under half a core, and kept sending a frame every 20 ms). The transport's CPU never saturated its two cores, so the
queue overflow is the single recv task being slower than the senders, not a starved device path. Still a guess: that a larger
`SO_RCVBUF` would reduce the reverse loss (not tried).

**Not measured.** A physically separate LAN host (the flood here is local delivery on one 4-core host, with the senders competing for
CPU with the transport and Firefox: no NIC, switch or interrupt cost, no real routing, the senders' source address is the host's
own); sources on more than two addresses; a real Android handset; Chromium; floods longer than 25 s; a flood of valid-looking
STUN with a correct ufrag (it would be accepted by design); the aarch64 binary. The flooder was not pinned away from the transport's
CPUs 0-1 (it ran at the same `nice 10` as the rest), so the transport's 46 to 53 % is with the senders contending for the same host.
