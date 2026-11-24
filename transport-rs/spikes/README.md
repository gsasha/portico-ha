# transport-rs/spikes: WebRTC library spike (DEV-764 S1, S2, S3, S1b)

**Throwaway evidence. Not part of the shipped crate.** The pion offerer (`peer-go/`) and the harness that drove it (`run_interop.py`, `run_scenarios.sh`, `run_long.sh`) were deleted in the Go cleanup (D-20261123-cc1e); they are in git history (commit `bdd8e61`), and the answerers below still build but have no driver in the tree. Nothing here is a member of a workspace with
`transport-rs/`, nothing in `../src`, `../tests` or `../Cargo.toml` depends on it, and it never touched
the Go transport. Its only job is to give the library decision (str0m, webrtc-rs, or a
pion helper) measured numbers. The results and the recommendation are in [RESULTS.md](RESULTS.md); the real-browser follow-up S1b (Firefox and
Chromium as offerers) is in [RESULTS_S1b.md](RESULTS_S1b.md).

| Directory / file | What it is |
|---|---|
| `answerer-str0m/` | The ANSWERER on `str0m =0.25.0` (sans-IO; the app owns the UDP socket and the clock). |
| `answerer-webrtc/` | The ANSWERER on `webrtc =0.21.1` (async API over the sans-IO `rtc` core; tokio). |
| `common/common.rs` | Shared by both answerers (`#[path]` include): mu-law, tone, the meter, the signalling helpers. |
| `s3-ffmpeg/` | S3: a tokio program that spawns `ffmpeg`, feeds it PCM on stdin, reads stdout. |
| `answerer-str0m/src/prod.rs`, `answerer-webrtc/src/prod.rs` | S1b: the production-shaped answerers (`prod` binary of each crate): PCMU only, `/1` rewrite, `catch_unwind`, per-interface host candidates in a non-trickle answer, mDNS-tolerant, many sessions per process. |
| `common/prod_common.rs` | S1b helpers shared by the two `prod` binaries: interface enumeration, offer rewrite and limits, spectrum meter, epoch-ms logging. |
| `browser/` | S1b: `page.html` (the REAL-browser offerer), `harness.py` (Python 3 stdlib: HTTP relay, browser launch and kill by exact PGID or named container, stats collection), `run_s1b.sh` (the scenario matrix), `summarise.py`, `build_glibc.sh` (cargo in docker with caches outside the worktree). |
| `hostile.py` | S1b scenario G: hostile and garbled offers against an answerer (survival, error replies, injected panic). |
| `liveness.py` | Liveness evidence from the unauthenticated GitHub API. |
| `build_musl.sh`, `rc.sh` | Docker-only builds (nothing is installed on the host). |

## Signalling protocol (line JSON over TCP on 127.0.0.1)

The answerer listens; each TCP connection is one session. Messages: `{"t":"offer","sdp":...}` (peer
to answerer; a second offer with new ICE credentials is the ICE restart), `{"t":"answer","sdp":...}`,
`{"t":"cand","c":"candidate:..."}` in both directions (both sides trickle: the answer SDP is sent
with its candidate lines stripped and the candidates follow), `{"t":"bye"}`. Both programs print one
JSON object per line: `SNAP` every `--snap` seconds, state-change events, and a final `REPORT`.

## Rebuild and rerun

Everything builds inside `docker run --rm` containers (no compose, no custom network, no host toolchain; crates are fetched over the default bridge):

```sh
cd transport-rs/spikes
./build_musl.sh answerer-str0m  str0m                # -> bin/str0m-x86_64, bin/str0m-aarch64   (static musl)
./build_musl.sh answerer-webrtc webrtc               # -> bin/webrtc-x86_64, bin/webrtc-aarch64
./build_musl.sh answerer-str0m str0m-rustcrypto --no-default-features --features rust-crypto   # RustCrypto primitives (aws-lc stays in the graph)
./build_musl.sh answerer-webrtc webrtc-awslc --no-default-features --features aws-lc          # aws-lc-rs instead of ring
python3 -I liveness.py algesten/str0m webrtc-rs/webrtc webrtc-rs/rtc
./s3-ffmpeg/run.sh
```

The harness picks a free port itself and writes per-process logs under `$SPIKE_LOGDIR` (default
`/home/agent/.claude/jobs/adec9d89/tmp/runs`). `bin/`, `target/`, `.cargo-home/` and `.cache/` are
git-ignored. Each Rust crate has its own `Cargo.toml` (empty `[workspace]` table, exact `=` pins) and
its own `Cargo.lock`.

## S1b: real browsers (Firefox and Chromium as offerer)

```sh
cd transport-rs/spikes
export S1B_CACHE=/somewhere/outside/the/worktree        # cargo home, target dirs, browser profiles, run logs
TGT=str0m-v2  ./browser/build_glibc.sh answerer-str0m  build --release --bin prod
TGT=webrtc-v1 ./browser/build_glibc.sh answerer-webrtc build --release --bin prod
SPIKE_LOGDIR=$S1B_CACHE/runs ./browser/run_s1b.sh $S1B_CACHE/target/str0m-v2/release/prod str0m firefox   # whole matrix
SPIKE_LOGDIR=$S1B_CACHE/runs ./browser/run_s1b.sh $S1B_CACHE/target/str0m-v2/release/prod str0m chrome    # needs docker + zenika/alpine-chrome
python3 -I hostile.py $S1B_CACHE/target/str0m-v2/release/prod
```

Firefox is `/usr/bin/firefox --headless` with a throwaway profile (a plain host process, killed by the process group of
the PID the harness started). Chromium runs in `docker run --rm --network host` only so that it can reach the
harness on 127.0.0.1; no Docker network is created. `prod` answerers take `--max-stun-retransmits N` (str0m only),
`--answer-delay-ms`, `--no-sdp-limits`, `--loopback-only`; env `S1B_INJECT_PANIC=1` (str0m) lets `hostile.py` prove the
`catch_unwind`. Same line-JSON signalling protocol as S1.
