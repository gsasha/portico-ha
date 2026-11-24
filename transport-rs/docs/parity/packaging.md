# transport-rs parity: packaging and size

Scope: packaging, release and rollback (Phase 4, build side) and the size and memory budget of the release binary.

The numbered differences of the whole port are indexed in [../../PARITY.md](../../PARITY.md); their numbers are stable (code comments, tests, the review log and the contract map cite them), so a new difference takes the next free number of its range and goes into the file of its subject. "Smoke" means `scripts/ipc_smoke.py` gave byte-identical output on both binaries, "unit" means a `cargo test` case. Reference: Go `transport/` at the branch point of `dev-764-rust-skeleton` (main `a52af72`), built with `golang:1.23`. Long table cells are shortened to a summary with the full text below the table.

## Size and memory (amd64, glibc dynamic, release profile `opt-level="s"`, LTO, strip)

Measured 2026-11-11, VmRSS read after 3 s idle with `-socket` only.

| Binary | Size | Idle RSS | Threads |
|---|---|---|---|
| Rust `transport-rs` (slice 1, no drivers) | 632 KB | 1.0 MB | 1 |
| Rust `transport-rs` (slice 3b-1, with the driver and the server; idle RSS not re-measured) | 975 KB | | |
| Rust `transport-rs` (slice 3b-2, with the outbound call; idle RSS not re-measured) | 1081 KB | | |
| Rust `transport-rs` (slice 4b, with str0m and `aws-lc-sys`; glibc, same profile) | 4.7 MB | 1.5 MB (listening); 4.0 MB with one configured `gate_sip_server`; 6.2 to 6.9 MB in a 60 s call (one session), 7.0 to 7.2 MB with two (INTEROP.md; VmRSS once a second) | |
| Rust `transport-rs` (slice 5a, with the observer; glibc, same profile, measured 2026-11-12) | 4,938,928 B (4.7 MiB) | 1.45 MB (listening, `-socket` only), 1 thread | 1 |
| Rust `transport-rs` (slice 4b), static `x86_64-unknown-linux-musl` | 4,876,232 B (ELF, `statically linked`; it ran scenario B with Firefox on the final source: idle 1.4 MB, 6.8 MB in the call) | | |
| Rust `transport-rs` (slice 4b), static `aarch64-unknown-linux-musl` | 4,068,792 B (ELF aarch64; links; NOT run, no qemu-user) | | |
| Rust `transport-rs` (slice 7a, with rustls), static `x86_64-unknown-linux-musl` | 6,215,624 B (static-pie, stripped; built with the recipe of `scripts/build_musl.sh` in `rust:1-bookworm`; `ipc_smoke.py unifi` on it is identical to Go's; aarch64 not rebuilt) | | |
| Go, plain `go build` | 15.1 MB | 8.2 MB | 8 |
| Go, `-ldflags="-s -w"` | 10.3 MB | not measured | |

The slice 1 figure had no drivers and no pion-equivalent and says nothing about in-call memory; the slice 4b rows have both. The Go in-call figure was not measured. The musl builds: `scripts/build_musl.sh <cache-dir>` (`rust:1-bookworm`, `musl-tools` for x86_64 and `gcc-aarch64-linux-gnu` for aarch64, `aws-lc-sys`'s C compiled by `cc`; about 2.3 minutes each on 4 cores).

## packaging, release and rollback (Phase 4): build side only

No Rust code changed. `packaging/rust/` (outside this crate; see its README) holds the Docker cross-build (`build_rust_binaries.sh`, x86_64 and aarch64 static musl, the compile with `--network none --offline --locked`), the wheel builder (`build_wheels.py`: `portico_transport` 0.7.x for the Rust lineage, the same `binary_path()` contract as the Go wheel plus `BACKEND`), the tag/version scheme (`transport-rs-v0.7.x` for Rust beside the existing `transport-v*` Go family; one shared version line), and `release_dryrun.py` (builds, writes `manifest.json` with sha256 sums, stops before any upload, tag or pin change).

* **Rollback by repin is a no-op, shown by tests** (`custom_components/portico/tests/test_wheel.py`, three new tests; `packaging/rust/tests/test_packaging.py` with real wheels and real pip). The mechanism built is the plan's first option: re-release the Go build under a version above every Rust version ever installed (`release_scheme.rollback_version`), tag it `transport-v<that>`, pin it; `door/wheel.py` is unchanged. The second option (the installer installs on any difference) is described, not built.
* **Deploy note (Phase 4).** With this the Rust binary can be BUILT, wrapped and released as a dry run for both architectures. It is still not installable by anyone but the maintainer: no wheel is uploaded, `WHEEL_PIN_*` is untouched, the dev-HA upgrade/downgrade rehearsal (spike S4) and the soak (G3) have not happened.
* **Not verified (Phase 4).** The aarch64 binary was never run (no qemu on this host); the wheels were installed with pip only on the host architecture, in a venv, with stand-in binaries for the pip test and the real x86_64 binary only inspected/started with `-h`; `door/wheel.py` was exercised with pip stubbed (as every test of it), so the real `uv pip install` of a URL requirement from the Gitea host was not run; the pin URL for aarch64 hosts (today the single x86_64 URL) is a decision for the pin PR.

> **Go sources removed (D-20261123-cc1e).** Go paths in this file, such as `transport/...`, `tools/gen_golden/...` and `scripts/golden_check.sh`, name the Go transport and the golden generators as they were at commit `bdd8e61`; they are deleted from `main` and live in git history only. The golden vectors in `tests/golden/` are frozen recordings. See [tests_and_vectors.md](../tests_and_vectors.md).
