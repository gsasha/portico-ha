# Real-browser interop spike S1b: results (DEV-764, gate G1)

Date of the runs: 2026-11-12. Follow-up to [RESULTS.md](RESULTS.md) (S1/S2/S3, pion offerer). This spike
makes REAL browsers the offerer and measures `str0m =0.25.0` (the provisional G1 choice) against
`webrtc =0.21.1` (the fallback). Throwaway code; how to rerun it: [README.md](README.md). Every number below was
measured in this session unless it says **not verified**.

**Setup.** Host: 4 cores, Debian 12, kernel 6.1. Answerers: glibc release builds (`rust:1-bookworm`, LTO,
stripped) of `answerer-str0m/src/prod.rs` and `answerer-webrtc/src/prod.rs`, started as plain host
processes. Browsers: **Firefox 153.6.0esr** (`/usr/bin/firefox --headless`, throwaway profile, prefs
`media.navigator.streams.fake=true`, `media.navigator.permission.disabled=true`, started as a host process in its
own process group, killed by that PGID) and **Chromium 124.0.6367.78** (`zenika/alpine-chrome:latest`, headless
(old mode), `docker run --rm --network host --name s1b-chrome-...`, `--use-fake-device-for-media-stream
--use-fake-ui-for-media-stream --no-sandbox`; killed with `docker kill` on that exact container name). The
offerer is `browser/page.html` (default codec order, Opus first), the harness `browser/harness.py` relays
offer/answer/trickle over HTTP on 127.0.0.1 to the answerer's line-JSON socket (the S1 protocol).
Machine interfaces (what the answerer enumerated and put into every answer): `lo` 127.0.0.1 and `::1`,
`eth0` 10.4.0.2 and the global IPv6 2a0d:6fc0:231c:7b06:be24:11ff:fe44:36a7, `docker0` 172.28.0.1,
`br-a78a7f8b6c31` 172.28.1.1 (6 host candidates; IPv6 link-local fe80:: is skipped on purpose).

## 1. Verdict

**str0m passed every scenario with both real browsers.** It answers PCMU only to an Opus-first offer, it connects
without any mDNS resolution (peer-reflexive), ICE restart is seamless, two sessions coexist, and the process
survived 31 hostile inputs (and a deliberately injected panic). Three things the port must own, none a blocker:
(a) **no `Failed` state and detection of a dead peer takes 21.9 to 24.2 s with the default setting; a smaller
`max_stun_retransmits` brings it to 10.6 to 15.8 s but not reliably to 10 s** (a hard-coded 15 s window, section 4);
the Go `house.Bridge` 10 s uplink-silence watchdog is what gives the 10 s bound; (b) **mDNS `.local` remote candidates cannot be parsed**
(logged and ignored; calls still connect because the browser's checks arrive as peer-reflexive candidates);
(c) oversized or pathological offers must be refused by a size limit (str0m itself needs 5 to over 20 s of CPU for an
offer with 20 000 to 50 000 candidate lines, and does not panic). webrtc-rs passed the same scenarios too; its
differences are listed in section 3.

## 2. The matrix (scenario x browser x library)

PASS means: connected, the browser's `getStats()` shows inbound PCMU packets rising, `audioLevel` > 0, 0 lost, the
browser's own FFT (WebAudio analyser on the remote stream) peaks at 441 Hz (our tone is 440 Hz), and the Rust side
received RTP of payload type 0 only. "FF" = Firefox 153 ESR, "CR" = Chromium 124. Rust-side "max gap" is the
longest inter-arrival time of PCMU packets at the answerer; "stall" is the browser-side longest time the inbound
packet counter did not move (100 ms sampling, so 100 to 140 ms means nothing was lost).

| Scenario | str0m FF | str0m CR | webrtc-rs FF | webrtc-rs CR |
|---|---|---|---|---|
| **A** plain call, 60 s | PASS. Browser rx 3009 pkts, 0 lost, jitter 3 ms, level 0.247, codec audio/PCMU, FFT 441 Hz. Rust rx 2961, PT 0 only, 0 lost, max gap 41 ms, tone 1000 Hz (Firefox's fake mic) in 58/58 s | PASS. Browser rx 3012, 0 lost, level 0.242, PCMU, 441 Hz. Rust rx 3053, PT 0 only, 0 lost, max gap 37 ms, 400 Hz beep in 54 s plus 5 s silent (Chrome's fake mic beeps) | PASS. Browser rx 3010, 0 lost; Rust rx 2963, 0 lost, max gap 41 ms | PASS. Browser rx 3000, 0 lost; Rust rx 3044, 0 lost, max gap 29 ms |
| **B** Opus-first offer, PCMU also offered: answer must be PCMU only | PASS. Offer order `109 opus, 9 G722, 0 PCMU, 8 PCMA, 101 telephone-event`; answer `m=audio 9 ... 0` + `a=rtpmap:0 PCMU/8000`. Browser reports codec audio/PCMU in and out | PASS. Offer order `111 opus, 63 red, 9 G722, 0 PCMU, 8 PCMA, 13 CN, 110 and 126 telephone-event`; answer PT 0 only | PASS (same answer) | PASS (same answer) |
| **C1** all trickle candidates sent BEFORE the offer (held, applied after `accept_offer`) | PASS, 15 s, 0 lost, rx 756 | PASS, 15 s, 0 lost, rx 763 | PASS, 0 lost | PASS, 0 lost |
| **C2** candidates arrive while the answer is held back 1500 ms (`--answer-delay-ms 1500`) | PASS, 0 lost | PASS, 0 lost | PASS, 0 lost (8 mDNS candidates "added") | PASS, 0 lost |
| **C3** non-trickle offer (candidates inside the SDP, no trickle) | PASS (embedded mDNS candidates ignored, no error) | PASS | PASS | PASS |
| **D** `pc.restartIce()` at 20 s, 45 s call | PASS, **no gap**: Rust max gap 44.7 ms, browser stall 142 ms, 0 lost; restart answer applied 62 ms after the new offer; ICE Checking, Completed again | PASS, no gap: Rust max gap 23.7 ms, stall 103 ms, 0 lost; answer applied after 15 ms | PASS. Rust lost 2 (browser 1), max gap 56.9 ms | PASS but audible: **13 packets lost, Rust max gap 280 ms, browser stall 299 ms** |
| **E** browser killed (exact PGID / container), default settings | Disconnected **21.9 s** after the kill; nothing after (no `Failed`) | Disconnected **24.2 s** after the kill | `disconnected` **5.0 s**, `failed` **30.2 s** | `disconnected` **4.7 s**, `failed` **29.9 s** |
| **E** with `set_max_stun_retransmits` | 5: 15.1, 15.8, 15.8 s; 4: 14.9 s; **3: 10.6, 13.4, 13.4 s** | 5: 15.1 s; 4: 15.3 s; 3: 14.8 s | no such knob | no such knob |
| **F** two browser processes, one answerer process | PASS: 2 sessions, rx 1453 and 1517, 0 lost, both PCMU | PASS: rx 1530 and 1530, 0 lost | PASS: 0 lost both | PASS: 0 lost both |
| **H1** mDNS host candidates ON (FF `obfuscate_host_addresses=true`; CR with an oscillator source, no getUserMedia) | PASS: 8 remote candidates all `.local`, 0 parsed, connected through a peer-reflexive pair, 0 lost | PASS: 1 `.local` candidate ignored, pair `prflx` local to our 127.0.0.1 host candidate, 0 lost | PASS: all 8 `.local` candidates "added" (Ok), connected | PASS |
| **H2** mDNS OFF (FF `obfuscate_host_addresses=false`; CR `--disable-features=WebRtcHideLocalIpsWithMdns`) | PASS: 8 candidates parsed and added (incl. TCP-active), 0 lost | PASS: 1 candidate added, pair 10.4.0.2 to 10.4.0.2 | PASS | PASS |
| **G** hostile offers (section 5) | survived all 31 hostile inputs, an error returned for each non-answerable one | same process (no browser involved) | survived all 31 | |

Notes on what the table does not say plainly:

* In every Firefox run the default configuration (even with `media.navigator.permission.disabled`) uses mDNS names;
  the selected pair is peer-reflexive from the browser to our global IPv6 host candidate. The Chrome
  getUserMedia runs also had one mDNS candidate (headless Chromium with `--use-fake-ui-for-media-stream` does not
  lift the obfuscation); the pair was `prflx` or host depending on the run, always connected within 0.2 s.
* The Firefox runs of str0m in the directories `str0m-firefox-*` (A, C1, C2, D, E, E3 to E5, F, H1, H2) used the first
  build of `prod`; everything else (all Chrome runs, `str0m-v2-firefox-C3`, `str0m-rep-*`, the hostile runs) used the
  final one. The second build only moved shared helpers into `common/prod_common.rs`, made a non-object JSON line an
  error and added the injected-panic hook; the media path is the same code. The `--no-sdp-limits` hostile run is from
  the first build.
* One Firefox start crashed (a headless Firefox minidump, unrelated to the answerer) in the first C3 run; the rerun
  (`str0m-v2-firefox-C3`) passed. Its `/tmp` crash dump was removed.
* The Rust side counts 33 RTP timestamp steps different from 160 in 60 s from Firefox's fake microphone (no packet
  lost, sequence continuous): not investigated, the Go jitter buffer orders by sequence number.

## 3. str0m versus webrtc-rs in these runs

| | str0m 0.25.0 | webrtc-rs 0.21.1 |
|---|---|---|
| Offer text | needs the `/1` rewrite for `PCMU/8000/1` (Firefox and Chrome already write `PCMU/8000`; Firefox writes `/1` on G722 and telephone-event, which are not enabled) | accepts both spellings |
| Answer | immediate (0 ms; host candidates are known up front), 6 candidates, `a=setup:passive`, `a=end-of-candidates` appended by the spike | waits for gathering (11 ms), 12 candidate lines (rtp and rtcp components of 6 addresses, although rtcp-mux is on), `a=setup:active`, `a=end-of-candidates` present |
| mDNS `.local` remote candidates | cannot be parsed ("invalid IP address syntax"): ignored, connects through prflx | `add_ice_candidate` returns Ok (resolution **not verified**), connects |
| ICE restart | seamless in both browsers | 56.9 ms gap with Firefox; 280 ms gap and 13 lost with Chrome |
| Dead peer | one `Disconnected` after 21.9 to 24.2 s, no `Failed` | `disconnected` after 4.7 to 5.0 s, `failed` after 29.9 to 30.2 s |
| Browser-side RTP jitter | 3 to 4 ms in Firefox runs (the spike's 10 ms pacing loop), 0 to 2 ms Chrome | 0 to 1 ms |
| Hostile offer | an error each; no panic found even with limits off; slow on 20k to 50k candidate lines | an error each, except it ANSWERS a duplicate-mid offer (two m-lines), an offer with garbage fingerprint and an `a=setup:bogus` |

## 4. Details that matter for the port

**Codec choice.** `RtcConfig::new().clear_codecs().enable_pcmu(true, false)`. With an Opus-first offer (both
browsers) the answer lists PT 0 only, and the browsers send PCMU (Rust counted only payload type 0 in all runs; the
browser stats say `audio/PCMU` for both directions). Normalisation: for every line starting `a=rtpmap:` that ends in
`/1`, drop the trailing `/1` (`normalise_offer` in `common/prod_common.rs`; `opus/48000/2` and `PCMU/8000` are
untouched). With the repo e2e rig's `PCMU/8000/1` offer the answer is PCMU (hostile case `GOOD-PCMU-8000-1`).

**Non-trickle answer.** The Go SFU waits for ICE gathering to complete before it returns the answer (the answer
holds every candidate). str0m has no gathering step for host candidates: the spike adds one host candidate per
interface address (a UDP socket bound to each of them, own enumeration with `if-addrs`) to the `Rtc` BEFORE
`accept_offer`, so the answer carries all of them; the spike appends `a=end-of-candidates`. Remote trickle is
accepted before the offer (held), while an answer is pending (C2), after it, as `a=end-of-candidates` / empty
string (C1, logged and ignored). Loopback and docker bridge addresses are included on purpose (like pion would); Chromium
used the 127.0.0.1 candidate in several runs.

**Dead-peer detection (E).** `max_stun_retransmits` is the only knob; it works but is not a clean 10 s lever.
Reading `is-0.12.0` (the ICE agent under str0m): a pair stays alive while the peer's own binding requests keep
arriving (constant `RECENT_BINDING_REQUEST` = 15 s, `pair.rs`), and otherwise until `ice_timeout()` = sum of
retransmit delays (250 ms doubling, capped at 3 s; for 9 retransmits 21.5 s). Browsers send a check about every 5 s,
so after a kill the detection is "last request + 15 s" or "last response + ice_timeout", whichever is later: with
9 it is 21.9 to 24.2 s measured; with 3 to 5 it collapses to the 15 s window less the phase of the last request
(10.6 to 15.8 s measured). Reaching a reliable 10 s needs a patch to that constant or an application watchdog;
the Go bridge already has the 10 s silence watchdog, and a media-silence rule would also catch a browser that is
alive but silent. This is a design limit of the current ICE agent, not a configuration error. I did not search
upstream for an issue about the 15 s constant. The source comment ties the constant to a Chrome v144+ behaviour
(Chrome answering consent checks asymmetrically); I could not test a Chrome that new (see section 7).

## 5. Hostile offers (G)

`hostile.py` sends each case on its own connection and then checks the process is still alive; at the end it sends a
good offer. 31 hostile inputs (26 offers, 5 at the signalling level) plus 2 good offers: empty, truncated in four places, no m-line, 4 KiB random bytes, NUL and control characters,
garbage lines, 3 MB of attributes, 100 000 attribute lines, 50 000 IPv4 candidates (3.9 MB), 20 000 IPv6 candidates,
duplicate mids, 200 m-lines (same mid and distinct mids), payload types out of range, m-port overflow, rtpmap without
slash, absurd rtpmap rate, a fingerprint of garbage, `a=setup:bogus`, 100 000 `a=ssrc` lines, 1 MB `fmtp`, LF-only line
endings, UTF-8 emoji, an injected panic inside `accept_offer`, and five signalling-level cases (non-JSON bytes, a JSON
array, an offer without `sdp`, `sdp` as a number, a 12 MB line).

* **str0m, final build, limits on (64 KiB / 1000 lines per offer, 8 MiB per signalling line): the process survived all
  33 requests; every input that is not a plausible offer got `{"t":"error"}`** (11 library parse or accept errors, 8 size
  refusals, 1 over-long signalling line, 4 other signalling refusals). 6 hostile inputs were still ANSWERED because str0m
  tolerates them: m-port overflow, rtpmap without slash, absurd rtpmap rate, `setup:bogus`, LF-only line endings, UTF-8
  in a cname (and the two good offers); the answer is a normal PCMU one.
* **Injected panic** (`S1B_INJECT_PANIC=1`, an offer containing `x-s1b-panic-now`): `catch_unwind` caught it, the
  session was closed with `{"t":"error","msg":"panic: ..."}`, the log shows `offer-PANIC-CAUGHT`, the next sessions
  worked. No real str0m 0.25.0 panic was found by the 31 inputs; the old issue #1111 is fixed in this version, so the
  catch is defence in depth, proven with the injection and not with a real trigger.
* **Limits off (`--no-sdp-limits`)**: the process still survives, no panic, but 50 000 candidate lines (3.9 MB)
  gave no reply within 20 s, 20 000 IPv6 candidates took 5.4 s, 100 000 `a=ssrc` lines gave no reply in 20 s
  (one session thread spinning on CPU; others unaffected). So the size limit is required, not cosmetic. An offer from a
  real browser is 1.2 to 1.3 KB (about 40 lines), a few KB with candidates.
* A reply is also not guaranteed by the library alone for a hung parse: `catch_unwind` cannot stop a thread, so the
  production code should run `accept_offer` on a worker with a deadline or keep the size limit.
* **webrtc-rs**: survived all 33 requests, an error for each (limits on), but it answered a duplicate-mid offer with two
  m-lines, and the garbage-fingerprint and `setup:bogus` offers. The injected-panic case is a no-op there (the hook is str0m's).
  Panic isolation there is a `tokio::spawn` per library call (a panic is a `JoinError`), not exercised for real.

## 6. Recommendation on G1

**G1 can be declared final for str0m, with a confidence of about 80 %.** Both browsers that exist in the lab
interoperate with it in every scenario asked, including the ones where I expected trouble (Opus-first, mDNS,
restart, kill, two sessions, hostile input). webrtc-rs also passes, so this is a preference rather than a rescue:
str0m wins on the ICE restart behaviour (no gap versus 280 ms and 13 lost packets with Chrome), the smaller graph and the
maintainer activity measured in RESULTS.md; webrtc-rs wins on a state machine that maps one to one onto the Go
`Failed`/`Closed` teardown and on mDNS names being accepted. Not a hybrid: nothing here needs a pion helper.

Conditions to write into the Rust port's design (all small): (1) the `/1` rewrite and a hard offer-size limit; (2)
`accept_offer` under `catch_unwind` in a per-session thread; (3) own the dead-peer rule: `Disconnected`
for N seconds is terminal, plus the existing uplink-silence watchdog; set `max_stun_retransmits` to 5 or less if
the 22 to 24 s default is too slow; (4) per-interface sockets and host candidates, enumerated at offer time,
including the Tailscale interface; (5) ignore `.local` candidates and rely on prflx, or implement mDNS resolution
if a real device turns out to need it.

**What would change my mind:** an Android WebView or the Portico app (libwebrtc) failing to connect through
peer-reflexive only (the case where the answerer cannot reach the phone's real address and the phone cannot reach any
of ours is not covered by a loopback lab: webrtc-rs's mDNS handling would then matter, and I did not verify that it resolves);
Safari failing on the offer; a current Chrome (144+) that does not keep a str0m pair alive; or a measured need for
a 10 s detection without a watchdog.

## 7. What I could not test

* **Safari / WebKit, Android WebView, the Portico Android app, iOS**: no such browser in the lab. Their
  offers resemble Chrome's (libwebrtc), but that is an inference.
* **A current Chrome.** The only Chromium available offline is the 124 build in `zenika/alpine-chrome:latest`; the
  str0m code comment about Chrome v144+ answering consent checks asymmetrically could not be exercised.
* **Tailscale interfaces, multi-subnet reachability and NAT**: this host has one LAN interface, the docker bridges and
  a global IPv6 address; every path is local (loopback, LAN address, same host). Whether a phone on the tailnet
  can reach the host candidates, and the one-way situation above, were not measured. IPv6 WAS present (a global
  address and `::1`) and Firefox selected the global IPv6 pair in most runs.
* **mDNS resolution itself** (multicast on a real LAN): neither library was verified to resolve names; str0m
  ignores them, webrtc-rs returns Ok.
* **Long calls with a browser** (only 60 s here; S1 had the 30-minute pion runs), audio quality (only a 440 Hz
  tone, FFT peak and packet counters), DTLS role problems with other offerers (the answerer was always passive in
  str0m), re-offer for adding a second m-line, video, and `max_stun_retransmits` on a lossy network.
* Chrome's `max_stun_retransmits` detection depends on `docker kill`, which can lag the first closed socket by a
  fraction of a second: the Chrome E numbers carry that.

## 8. Exact commands

```sh
cd transport-rs/spikes
# build (glibc, in docker, all caches outside the worktree; rust:1-bookworm)
TGT=str0m-v2  ./browser/build_glibc.sh answerer-str0m  build --release --bin prod   # -> $S1B_CACHE/target/str0m-v2/release/prod
TGT=webrtc-v1 ./browser/build_glibc.sh answerer-webrtc build --release --bin prod
# the matrix, one browser at a time (A C1 C2 C3 D E E5 E4 E3 F H1 H2); results land in $SPIKE_LOGDIR
SPIKE_LOGDIR=... ./browser/run_s1b.sh <answerer-binary> <label> firefox
SPIKE_LOGDIR=... ./browser/run_s1b.sh <answerer-binary> <label> chrome
# one run by hand
python3 -I browser/harness.py --answerer <bin> --browser firefox --scenario D --duration 45 --restart-at 20
python3 -I browser/harness.py --answerer <bin> --browser firefox --scenario E --duration 300 --kill-at 15 \
        --answerer-arg=--max-stun-retransmits --answerer-arg=3
python3 -I browser/harness.py --answerer <bin> --browser firefox --scenario H2 --duration 15 --obfuscate false
python3 -I browser/harness.py --answerer <bin> --browser chrome  --scenario H2 --duration 15 \
        --chrome-flag=--disable-features=WebRtcHideLocalIpsWithMdns
# hostile offers
python3 -I hostile.py <answerer-binary>                        # limits on
python3 -I hostile.py <answerer-binary> --no-sdp-limits        # str0m's raw behaviour
S1B_INJECT_PANIC=1 python3 -I hostile.py <str0m prod binary>   # proves catch_unwind
python3 -I browser/summarise.py <run dir> ...                  # compact view of a run
```

Chromium is started by the harness as `docker run --rm --name s1b-chrome-<pid>-<i> --network host
zenika/alpine-chrome:latest --no-sandbox --disable-gpu --disable-dev-shm-usage --use-fake-device-for-media-stream
--use-fake-ui-for-media-stream --autoplay-policy=no-user-gesture-required --no-first-run
--user-data-dir=/tmp/chrome-profile --remote-debugging-port=933<i> <url>` (old headless mode only navigates when a
debugging port is given; it is bound to 127.0.0.1). Firefox: `firefox --headless --no-remote --profile <dir> <url>`
with `user.js` prefs: fake media, `media.peerconnection.ice.obfuscate_host_addresses` (toggled),
`media.peerconnection.ice.loopback=true`. No Docker network was created; `--network host` was used only for the
Chromium `docker run --rm`.

## 9. SDP of one real Firefox call and one real Chromium call (str0m answerer, run A)

**Firefox 153 ESR offer (candidates trickled afterwards; all of them mDNS `.local`)**

```
v=0
o=mozilla...THIS_IS_SDPARTA-99.0 4141569745251053772 0 IN IP4 0.0.0.0
s=-
t=0 0
a=fingerprint:sha-256 CE:BD:A8:9B:8B:ED:24:02:C7:91:4E:4E:FA:B1:DC:2C:3D:57:E7:1A:E6:E5:EF:FD:C5:3B:3F:D9:2C:8F:FC:7C
a=group:BUNDLE 0
a=ice-options:trickle
a=msid-semantic:WMS *
m=audio 9 UDP/TLS/RTP/SAVPF 109 9 0 8 101
c=IN IP4 0.0.0.0
a=sendrecv
a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level
a=extmap:2/recvonly urn:ietf:params:rtp-hdrext:csrc-audio-level
a=extmap:3 urn:ietf:params:rtp-hdrext:sdes:mid
a=extmap:7 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01
a=extmap-allow-mixed
a=fmtp:109 maxplaybackrate=48000;stereo=1;useinbandfec=1
a=fmtp:101 0-15
a=ice-pwd:9ecc012348dd3df6ee4a75f15b77d16c
a=ice-ufrag:f00eacf9
a=mid:0
a=msid:{17588704-50d2-429e-a168-5c73643d2c65} {c2daeea9-9cee-4ac0-8bad-0418d7c4066e}
a=rtcp-fb:109 transport-cc
a=rtcp-fb:9 transport-cc
a=rtcp-fb:0 transport-cc
a=rtcp-fb:8 transport-cc
a=rtcp-mux
a=rtpmap:109 opus/48000/2
a=rtpmap:9 G722/8000/1
a=rtpmap:0 PCMU/8000
a=rtpmap:8 PCMA/8000
a=rtpmap:101 telephone-event/8000/1
a=setup:actpass
a=ssrc:3734187237 cname:{177684f0-0240-4b42-b740-23a4c55d3805}
```

**str0m answer to the Firefox offer (non-trickle: six host candidates inside, PCMU only)**

```
v=0
o=str0m-0.25.0 3321027048903259070 2 IN IP4 0.0.0.0
s=-
t=0 0
a=group:BUNDLE 0
a=extmap-allow-mixed
a=msid-semantic: WMS {17588704-50d2-429e-a168-5c73643d2c65}
m=audio 9 UDP/TLS/RTP/SAVPF 0
c=IN IP4 0.0.0.0
a=candidate:fffeff7e32c583ef85edbb1a 1 udp 2130706175 127.0.0.1 58540 typ host ufrag GYCLKUogp9lNnXR1
a=candidate:fffcff7eb51266b2d6365a4e 1 udp 2130705663 10.4.0.2 53176 typ host ufrag GYCLKUogp9lNnXR1
a=candidate:fffaff7e9bdaec639b566413 1 udp 2130705151 172.28.0.1 42614 typ host ufrag GYCLKUogp9lNnXR1
a=candidate:fff8ff7e792489ea1260907a 1 udp 2130704639 172.28.1.1 40003 typ host ufrag GYCLKUogp9lNnXR1
a=candidate:ffffff7ed7a94546d917f5d6 1 udp 2130706431 ::1 59076 typ host ufrag GYCLKUogp9lNnXR1
a=candidate:fffdff7e8719f8e282d8ec13 1 udp 2130705919 2a0d:6fc0:231c:7b06:be24:11ff:fe44:36a7 44183 typ host ufrag GYCLKUogp9lNnXR1
a=ice-ufrag:GYCLKUogp9lNnXR1
a=ice-pwd:Nvvvz9uEtQFkeQddc5o23v
a=ice-options:trickle
a=fingerprint:sha-256 5D:B2:9D:77:3F:4D:AE:0D:56:B8:EA:30:B0:3D:91:1B:D8:45:7A:E5:0C:0E:69:AE:36:28:4B:B5:B8:37:72:34
a=setup:passive
a=mid:0
a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level
a=extmap:3 urn:ietf:params:rtp-hdrext:sdes:mid
a=extmap:7 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01
a=sendrecv
a=msid:{17588704-50d2-429e-a168-5c73643d2c65} {c2daeea9-9cee-4ac0-8bad-0418d7c4066e}
a=rtcp-mux
a=rtpmap:0 PCMU/8000
a=rtcp-fb:0 transport-cc
a=ssrc:1111727866 cname:ZZXj47TFnQGXoJ5kxiel
a=ssrc:1111727866 msid:{17588704-50d2-429e-a168-5c73643d2c65} {c2daeea9-9cee-4ac0-8bad-0418d7c4066e}
a=end-of-candidates
```

**Chromium 124 offer**

```
v=0
o=- 4178527616007668059 2 IN IP4 127.0.0.1
s=-
t=0 0
a=group:BUNDLE 0
a=extmap-allow-mixed
a=msid-semantic: WMS adfc3ebb-be30-4d57-a058-67dca34daeca
m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8 13 110 126
c=IN IP4 0.0.0.0
a=rtcp:9 IN IP4 0.0.0.0
a=ice-ufrag:1oxG
a=ice-pwd:giVyQNXgXVjIJP2Nkhk9o/XD
a=ice-options:trickle
a=fingerprint:sha-256 11:42:87:FD:CF:8D:55:27:06:37:36:F8:05:EF:28:CD:4A:E4:AE:36:B9:F3:F3:AA:B9:29:4A:EC:3A:16:9F:52
a=setup:actpass
a=mid:0
a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level
a=extmap:2 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time
a=extmap:3 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01
a=extmap:4 urn:ietf:params:rtp-hdrext:sdes:mid
a=sendrecv
a=msid:adfc3ebb-be30-4d57-a058-67dca34daeca ad7144f7-933f-4a57-a102-c430af026ec3
a=rtcp-mux
a=rtpmap:111 opus/48000/2
a=rtcp-fb:111 transport-cc
a=fmtp:111 minptime=10;useinbandfec=1
a=rtpmap:63 red/48000/2
a=fmtp:63 111/111
a=rtpmap:9 G722/8000
a=rtpmap:0 PCMU/8000
a=rtpmap:8 PCMA/8000
a=rtpmap:13 CN/8000
a=rtpmap:110 telephone-event/48000
a=rtpmap:126 telephone-event/8000
a=ssrc:3154020972 cname:BZ5Fm47CHuW1Oypz
a=ssrc:3154020972 msid:adfc3ebb-be30-4d57-a058-67dca34daeca ad7144f7-933f-4a57-a102-c430af026ec3
```

**str0m answer to the Chromium offer**

```
v=0
o=str0m-0.25.0 3823495054460672299 2 IN IP4 0.0.0.0
s=-
t=0 0
a=group:BUNDLE 0
a=extmap-allow-mixed
a=msid-semantic: WMS adfc3ebb-be30-4d57-a058-67dca34daeca
m=audio 9 UDP/TLS/RTP/SAVPF 0
c=IN IP4 0.0.0.0
a=candidate:fffeff7e32c583ef85edbb1a 1 udp 2130706175 127.0.0.1 37274 typ host ufrag Od6GxKERPchN3Gaa
a=candidate:fffcff7eb51266b2d6365a4e 1 udp 2130705663 10.4.0.2 42061 typ host ufrag Od6GxKERPchN3Gaa
a=candidate:fffaff7e9bdaec639b566413 1 udp 2130705151 172.28.0.1 39281 typ host ufrag Od6GxKERPchN3Gaa
a=candidate:fff8ff7e792489ea1260907a 1 udp 2130704639 172.28.1.1 48011 typ host ufrag Od6GxKERPchN3Gaa
a=candidate:ffffff7ed7a94546d917f5d6 1 udp 2130706431 ::1 46337 typ host ufrag Od6GxKERPchN3Gaa
a=candidate:fffdff7e8719f8e282d8ec13 1 udp 2130705919 2a0d:6fc0:231c:7b06:be24:11ff:fe44:36a7 55764 typ host ufrag Od6GxKERPchN3Gaa
a=ice-ufrag:Od6GxKERPchN3Gaa
a=ice-pwd:PQaODp192PXQ6di2lkADLn
a=ice-options:trickle
a=fingerprint:sha-256 04:19:04:EF:25:D6:36:BE:9C:53:30:44:51:EC:D0:24:5F:2A:D2:34:E9:58:EE:FD:60:CF:64:0C:AC:E2:A1:D6
a=setup:passive
a=mid:0
a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level
a=extmap:2 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time
a=extmap:3 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01
a=extmap:4 urn:ietf:params:rtp-hdrext:sdes:mid
a=sendrecv
a=msid:adfc3ebb-be30-4d57-a058-67dca34daeca ad7144f7-933f-4a57-a102-c430af026ec3
a=rtcp-mux
a=rtpmap:0 PCMU/8000
a=ssrc:182269011 cname:Ke9LRqVj0UGdhSgzU3ca
a=ssrc:182269011 msid:adfc3ebb-be30-4d57-a058-67dca34daeca ad7144f7-933f-4a57-a102-c430af026ec3
a=end-of-candidates
```

The webrtc-rs answer to the same Firefox offer, for comparison, was `m=audio 9 UDP/TLS/RTP/SAVPF 0` with
`a=setup:active`, `a=rtcp-mux`, `a=rtcp-rsize`, `a=rtpmap:0 PCMU/8000`, `a=rtcp-fb:0 transport-cc`,
`a=sendrecv`, and 12 `a=candidate:` lines (components 1 and 2 of each of the six addresses) then
`a=end-of-candidates`. The raw logs, SDPs (`sdp.json`) and per-second `getStats()` series (`reports.json`) of every
run are under `/home/agent/.claude/jobs/adec9d89/tmp/cache/runs/` and are not committed.

## Review addendum (independent review, 2026-11-12) and the parent's reading

An independent read-only review traced the matrix to the run directories (`result.json`, `sdp.json`, `reports.json`, `answerer.log`) and confirmed the PASS criteria are strong (browser `audio/PCMU`, `audioLevel` about 0.25, FFT peak 441 Hz, Rust side `pt_counts {'0': N}`, loss and gap), the Opus-first offers with PCMU-only answers (`sdp.json`), the 33 hostile cases with the process alive after each, and the 15 s `RECENT_BINDING_REQUEST` window (`is-0.12.0/src/pair.rs:12`, used at `pair.rs:484`; `set_initial_stun_rto` and `set_max_stun_rto` cannot go below it either). Findings, all accepted:

- **The codec set changed under the spike (changes scope).** Go PR #282 (G.722 wideband plus PCMU on the phone leg) landed on 2026-11-11 evening. S1 and S1b tested PCMU only. str0m offers `enable_g722`, but no Rust G.722 codec crate has been chosen and G.722-first negotiation was never tested. Required before the SFU port is merged: choose a G.722 implementation (the Go side added `internal/sfu/g722.go`, 151 lines, plus a `go.mod` dependency: read it), test that Firefox and Chromium offers are answered with G.722 first and PCMU as fallback, and carry real audio both ways.
- **The Go 10 s silence watchdog does not cover the gate path.** It lives in `internal/house` (phone-to-phone bridges). The browser-to-gate path is `cmd/portico-transport/main.go` `sessionRegistry`, which ends only on `OnDisconnected`, and pion fires that only on `Failed` or `Closed` (about 30 s today, `sfu.go:187`). str0m has no `Failed`, so a plain port would never tear down a call whose browser vanished. The port needs its own terminal rule: `Disconnected` plus a grace timer (and/or an uplink-silence check) on the gate path; `max_stun_retransmits` alone gives 10.6 to 15.8 s at best.
- **Interface allow-list.** Go uses `NewSession(callID)` with no SettingEngine: pion defaults, not "loopback like the spike". The spike advertised `lo`, `eth0`, `docker0`, a bridge and a global IPv6 (a lab artefact; several Chrome pairs ended on 127.0.0.1). The port should use an explicit allow-list: no loopback, no docker bridges, a deliberate decision about the tailnet addresses (`100.x`, `fd7a:`), which also depends on whether the transport runs on host networking. Tailscale was not tested.
- **mDNS** works here only through peer-reflexive candidates and every test path was on one host; a different L2 segment, NAT or cellular via the tailnet is untested. The Android app (native libwebrtc) likely does not obfuscate with mDNS, which lowers the risk for the real client.
- **Build provenance.** Firefox runs A, C1, C2, D, E, F, H1, H2 used the first str0m build; only C3 and the E3/E5 repeats are on the final build (all Chrome and hostile runs are final). Rerun Firefox A and D on the final build.
- **Hostile offers:** limits-on runs only tried oversized inputs; a hostile offer just under 64 KiB / 1000 lines that is expensive for str0m was not run. `catch_unwind` covers `accept_offer` and the session thread, so a real str0m panic in the media loop ends that session, not the process: acceptable, state it. No `panic=abort` anywhere; **the main crate must pin `panic = "unwind"` explicitly** in `transport-rs/Cargo.toml`.
- `str0m-chrome.summary` and `str0m-rep-firefox.summary` show the same run dir for E3/E4/E5 (a `summarise.py` display bug); the per-run `result.json` files hold the right numbers.

**Parent's reading: G1 is final for str0m at about 70%** (the builder said 80%; the reviewer said about 70% is defensible until the Android app is tested). The library interoperates with real Firefox and Chromium for every scenario tried; what remains is port-time work and one real-client risk: (1) G.722 codec and negotiation, (2) the terminal rule for `Disconnected` on the gate path, (3) the interface allow-list and the tailnet, (4) the Android app and Safari as offerers over the tailnet or cellular, to be covered in the Phase 5 rehearsal. webrtc-rs stays the fallback if (4) fails; the hybrid is not needed.
