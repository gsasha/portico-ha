# transport-rs

A Rust port of the Go `portico-transport` binary (task DEV-764; the Go source, formerly `../transport/`, was deleted after the cut-over, D-20261123-cc1e, and is in git history, commit `bdd8e61`; plan:
`automation/portico_rust_transport_plan.md` in the estate repo). The crate builds a binary with the
same name, flags and IPC as the Go one: the hub (`custom_components/portico/`) starts it, speaks line
JSON to it over a Unix socket (or, in standalone mode, a token-authenticated TCP socket), and it carries
the SIP drivers (`gate_sip_server`, `dahua_sip`, `unifi_protect`), the WebRTC/SFU
audio path, the away phone's WebSocket media and the house-call bridge. It is the transport the
`portico_transport` wheel ships and `custom_components/` runs (cut over 2026-11-17, DEV-764). Where the port differs from Go on purpose, [PARITY.md](PARITY.md) says so.

## Repository layout

A newcomer's map of `transport-rs/` (nothing here is moved or removed; "archive" marks material kept as
evidence, not part of what ships):

| Path | What it is |
|---|---|
| `Cargo.toml`, `Cargo.lock`, `rust-toolchain.toml` | the crate `portico-transport` (library `transport_rs` plus the `portico-transport` binary); all dependencies pinned |
| `src/` | **the crate that ships.** `main.rs`, `ipc/` (the IPC server and TCP mode), `dispatch/` (the request methods and the device registry), `process/` (flags, panic hook, secrets, standalone), `sip/` (the SIP library), `driver/` (the five drivers), `sfu/`, `call/`, `webrtc.rs` (WebRTC session and call sequencing), `wsmedia.rs`, `house/` (the house-call bridge), `gojson.rs` and `gostd/` (Go's JSON and standard-library texts), `cancel.rs`, `pacer.rs`, `util.rs` |
| `tests/` | integration tests (`cargo test`), one file per subject, shared fakes in `tests/common/`; `tests/golden/` holds the vectors recorded from the real Go code and replayed by the `golden_*.rs` tests; `tests/rehearsal/` the recorded gate-sip dialogues; `tests/interop/` the real-browser harness (Firefox and Chromium as offerers, results in `INTEROP.md`) |
| `scripts/` | Python 3 (standard library only): `contract_runner.py` (the language-neutral contract and the PARITY map audit), `ipc_smoke.py` (the byte-for-byte smoke script run on both binaries), the helpers `trace_sip.py` and `tracelib.py` (left from the retired `wire_deferred.py`, DEV-831; its `D-*` case ids now live in `contract_runner.py` `DEFERRED_CASE_IDS`), `build_musl.sh` |
| `conformance/` | recordings made from the Go binary (`transcripts/`, the Rust patches in `transcripts/rust_patches.json`) and the trace-diff traces and results (`traces/`, `TRACEDIFF.md`, `deferred_wire_results.json`) |
| `../contract/` | the contract itself (`transport_ipc_v1.json`) and `transport_ipc_v1_parity_map.json`, which maps every numbered difference in `docs/parity/` to a runner case or a reason |
| `../packaging/rust/` | the static musl cross-build, the wheel builder and the dry-run release (not part of this crate) |
| `DESIGN.md`, `PARITY.md`, `INTEROP.md`, `docs/` | design and task model, the parity index, the browser interop runs, and the documentation listed below |
| `spikes/` | **archive.** The WebRTC library spike (str0m against webrtc-rs against a pion helper) that decided the library; throwaway code, no part of the crate or its tests; read `spikes/README.md`, `RESULTS.md`, `RESULTS_S1b.md` for the evidence |

**What is archive material.** `spikes/` (the library decision), `conformance/traces/` (recorded runs, kept
as the evidence behind `conformance/TRACEDIFF.md`), `tests/interop/` runs recorded in `INTEROP.md`, and
the per-slice history in `docs/slices.md`. They are not read by the build. What is live: `src/`, `tests/`
(including `tests/golden/`, which the tests read), `scripts/` and `conformance/transcripts/`
(read by the contract runner), and `../contract/`.

## Run

```sh
portico-transport -socket /run/portico/transport.sock      # the hub's mode: IPC over a Unix socket
portico-transport -listen 127.0.0.1:9000 -ipc-token-file F # standalone mode: token-authenticated TCP
```

`portico-transport -h` lists the nine flags (same names and defaults as the Go binary). The protocol
(every request, notification and error code) is `../contract/transport_ipc_v1.json`. Nothing here is
installed on the host by this repository: build it as below.

## Build and test

No cargo is installed on the host; use the Rust image (run from the repository root):

```sh
W=$PWD/transport-rs
R="docker run --rm -u $(id -u):$(id -g) -v $W:/work -w /work -e CARGO_HOME=/work/.cargo-home rust:1-bookworm"
$R cargo test
$R cargo build --release
```

Static release builds for x86_64 and aarch64 (musl; locked, offline, source mounted read-only, as the build user):
`../packaging/rust/build_rust_binaries.sh OUT_DIR`. `scripts/build_musl.sh` only forwards to it.

`clippy` and `rustfmt` are not in the image. Install them into a private rustup home (ignored by
git) once, then run them:

```sh
R2="docker run --rm -u $(id -u):$(id -g) -v $W:/work -w /work -e CARGO_HOME=/work/.cargo-home -e RUSTUP_HOME=/work/.rustup rust:1-bookworm"
$R2 sh -c 'mkdir -p .rustup && cp -r /usr/local/rustup/. .rustup/ && rustup component add clippy rustfmt'
$R2 cargo clippy --all-targets -- -D warnings
$R2 cargo fmt --check
```

**The `test-seams` feature.** The setters, accessors and panic-injection hooks the tests reach into the
production types with exist only with the cargo feature `test-seams` (`src/seams.rs` and every item
marked `#[cfg(feature = "test-seams")]`). The crate's own dev-dependency switches it on, so `cargo test`
and `cargo clippy --all-targets` have it and `cargo build --release` does not (a shipped binary reads
no injection flag, not even on a hot path). Check the shipped configuration with
`cargo clippy --lib --bins -- -D warnings`. The lint policy is the `[lints]` table of `Cargo.toml`.

**The binary `cargo test` leaves is the seams build.** The self dev-dependency turns `test-seams` on for every
target of the package under `cargo test`, so the `portico-transport` that the integration tests spawn
(`CARGO_BIN_EXE_portico-transport`) and that `cargo test` leaves at `target/debug/portico-transport` is NOT the
program that ships: it holds the injection hooks (strings such as `injected receive error`), and no cargo test
compiles the `cfg(not(feature = "test-seams"))` branches. The shipped configuration is exercised by this gate,
which runs after the tests (R2-35):

```sh
$R cargo build --release                          # the shipped configuration (no features, optimised)
scripts/shipped_gate.sh target/release/portico-transport    # prints 0 and exits 0 (R3B-2; a debug build prints more: see below)
python3 scripts/contract_runner.py --impl rust target/release/portico-transport --self-test   # then without --self-test
```

The gate names the RELEASE binary on purpose (R3B-2): twelve injection sites (`if flag.take() { panic!("injected ..") }`,
`if cfg.fault_is(..) { panic!(..) }`) are not `cfg`-gated, they are branches on a constant `false` that the optimiser removes;
a features-off DEBUG build keeps their text (5 lines of `strings`), so `strings target/debug/portico-transport | grep -c injected`
is not 0 and is not the shipped program. The gated hooks (the dahua ones, `injected receive error`, `injected by a test`, the
test API key) are absent from both profiles.

Build it with a plain `cargo build --release`, never in the same invocation as `--examples`, `--tests` or `--all-targets`: those activate the dev-dependencies and with them `test-seams`, and the binary then holds all the injection text (found in round 3: 19 hits).

Never hand the binary a `cargo test` left to `ipc_smoke.py` or the contract runner: rebuild first. The nonce
clock of `gate_sip_server` is the one fork of behaviour that used to differ between the two builds; both now run
the same `NonceStore` code, and only the setter that moves its clock is gated.

`cargo build --release` here gives a glibc-linked binary. The static musl builds for x86_64 and aarch64 are
`../packaging/rust/build_rust_binaries.sh` (`scripts/build_musl.sh` forwards to it).

## Contract and conformance runner (Phase 2)

`../contract/transport_ipc_v1.json` is the language-neutral IPC contract (every request and
notification with its error codes, the nine flags and the environment, exit codes, socket mode, signals,
the log prefix, the panic policy, the capabilities of the five drivers, and the documented
Go-versus-Rust differences). `scripts/contract_runner.py` drives either binary against it:

```
# the Rust binary: cargo build --release as above, then copy target/release/portico-transport
# (the Go side is gone: its source is only in git history, commit bdd8e61 / tag transport-v0.6.10; the recorded Go transcripts stay as the reference)

python3 scripts/contract_runner.py --impl rust <the Rust binary>
python3 scripts/contract_runner.py --impl rust <binary> --self-test    # deliberately wrong expectations must FAIL
python3 scripts/contract_runner.py --audit                             # patches vs documented differences; PARITY map vs PARITY.md
```

Layers: `W-*` wire cases and the empty-hub error table, `S-*` soft contracts, `T-*` the long flows of
`ipc_smoke.py` compared with `conformance/transcripts/*.txt` (recorded from the Go binary; the Rust
binary is compared after `rust_patches.json`, each patch tied to a `DIFF-*` entry of the spec and to its
PARITY row), `C-*` the capabilities per driver. `--record` rewrites the transcripts (Go) or the patches
(Rust, and it refuses a difference no `DIFF-*` entry covers). Running a binary under the wrong
`--impl` fails the groups that differ (webrtc, standalone): that is the discrimination check on the
real behaviour. The runner binds fixed loopback addresses (127.0.0.9, 127.0.0.10, port 5060): never run two
runs at once. `../contract/transport_ipc_v1_parity_map.json` maps every numbered PARITY.md row to a case or to
a reason code.

## Documentation map

| Where | What |
|---|---|
| [PARITY.md](PARITY.md) | **index** of every numbered difference from the Go binary: number, subject file, one-line summary. Numbers are stable (code, tests, the review log and the contract map cite them) |
| [docs/parity/](docs/parity/) | the detail, one file per subject: `ipc_process`, `sip`, `gate_sip_server`, `webrtc_sfu`, `gate_sip_observer` (retired, history only), `dahua`, `unifi_protect`, `wsmedia`, `house`, `standalone`, `packaging` |
| [DESIGN.md](DESIGN.md) | the task and cancellation design (goroutine/channel/context to tokio), runtime and shutdown |
| [INTEROP.md](INTEROP.md) | the runs against real Firefox and Chromium |
| [docs/slices.md](docs/slices.md) | the slices in the order they were built, the module status table |
| [docs/tests_and_vectors.md](docs/tests_and_vectors.md) | the test inventory, the frozen golden vectors (their generators were deleted), the smoke comparison with the Go binary, per-slice test notes |
| [docs/dependencies.md](docs/dependencies.md) | why each dependency is there |
| [conformance/TRACEDIFF.md](conformance/TRACEDIFF.md) | the Go-versus-Rust timing trace-diff and its findings |
| [spikes/README.md](spikes/README.md) | the WebRTC library spike (archive) |
