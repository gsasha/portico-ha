# Packaging the Rust transport (DEV-764 Phase 4: the BUILD side)

Status: **built and tested offline; nothing published, tagged, pinned or installed.** Publishing a release, changing the pin in
`custom_components/portico/door/wheel.py`, installing a Rust wheel on any live Home Assistant and retiring Go are the maintainer's
sitting (GATE in `docs/tasks/portico-rust-transport.md`, plan section 9 Phase 4/5/6).

| File | What it is |
| :--- | :--- |
| `Dockerfile.build` | Local build image: `rust:<exact version>-bookworm@sha256:<digest>` (pinned, R1-48) + the two musl targets + version-pinned `musl-tools` + the aarch64 cross gcc with its libc headers. Built once online. |
| `build_rust_binaries.sh` | Docker cross-build of `transport-rs/` for x86_64 and aarch64 (static musl). Output: `portico-transport-linux-amd64`, `-arm64`, `SHA256SUMS`, `build-info.txt`. |
| `build_wheels.py` | Builds the `portico_transport` wheels (`rust` only; the Go backend was removed) from a directory of binaries. Stdlib only, no network, byte-reproducible. |
| `release_scheme.py` | The tag and version scheme (pure functions). |
| `release_dryrun.py` | Builds everything, writes `manifest.json` + `SHA256SUMS`, STOPS. No upload, tag, pin change or non-read-only git. |
| `../standalone/Dockerfile.rust` | The standalone container image around the Rust binary (the only standalone image since the Go `Dockerfile` was removed). |
| `tests/` | `python3 -m pytest packaging/rust/tests` (stdlib + pytest + the host's pip; no Docker, no network). |

## 1. The cross-build

```sh
packaging/rust/build_rust_binaries.sh /tmp/portico-rs-out                   # first time: builds the image, fetches crates, compiles both
packaging/rust/build_rust_binaries.sh --no-fetch --arch aarch64 OUT         # later, offline: one architecture
packaging/rust/build_rust_binaries.sh --fetch-only                          # warm the cargo home, then go offline
```

* **Network use is one step.** The image build (apt, `rustup target add`) and `cargo fetch --locked` are the only online steps.
  The compile runs `docker run --network none` with `cargo build --release --locked --offline` and the crate mounted read-only,
  so a cold cache fails loudly instead of reaching out. No network is created: the containers use Docker's default modes only.
* **Where things go.** Cargo home, target dir and `HOME` live under `PORTICO_RS_CACHE` (default `~/.cache/portico-rs`;
  override `PORTICO_RS_CARGO_HOME` / `PORTICO_RS_TARGET_DIR`). The worktree is never written. The user inside the container is
  the caller's uid/gid, so nothing root-owned is left behind. A cold x86_64 + aarch64 build takes about 10 minutes on 4 cores and
  the target dir is about 2 GB (delete it when done; the cargo home is the part worth keeping).
* **Reproducibility.** `--locked`, `CARGO_INCREMENTAL=0`, `SOURCE_DATE_EPOCH` = the commit time, `--remap-path-prefix` for the
  source, cargo home and target dir, `strip`/LTO from `[profile.release]`. Compare two runs' `SHA256SUMS`: the result of such a double build is committed below (section "Double-build result"), and `PORTICO_RS_REAL_BUILD=1 python3 -m pytest packaging/rust/tests -k real_double_build` repeats it (opt-in, Docker, about 10 minutes per architecture; needs a warm cargo home or `PORTICO_RS_FETCH=1`).
* **Pinned toolchain (R1-48).** The base image is pinned by exact rustc version and digest, the three apt packages by version, and `transport-rs/rust-toolchain.toml` names the same rustc. Changing any of them means changing all of them and the image tag (`local/portico-rs-build:N` in `Dockerfile.build` and `build_rust_binaries.sh`), because a built tag is reused. Transitive apt dependencies are not pinned; Debian removes superseded versions from its live mirror, so an old pin fails to build rather than building something different.
* **Static check.** `readelf -lW` and `readelf -d` run inside the build image on the built binary; a non-zero exit, `INTERP` or `NEEDED` fails the build.
* **Toolchain.** x86_64: `musl-gcc` compiles the C of `aws-lc-sys`. aarch64: `aarch64-linux-gnu-gcc` compiles it and links, while rustc
  supplies the self-contained musl libc (`transport-rs/scripts/build_musl.sh` only forwards to this script).
  `--no-install-recommends` drops the cross libc headers and the aarch64 build then fails on `bits/libc-header-start.h`; the
  Dockerfile names `libc6-dev-arm64-cross` for that reason.

### Double-build result

Two independent builds (separate target dirs, same cargo home, `--no-fetch` offline compile, both architectures) gave byte-identical binaries.
Made with `ARG RUST_IMAGE=rust:1.99.1-bookworm@sha256:e6f2cff35d0f309acf8433b4f05878ed77092602c5065cc81801177da0bb3a9c` (the build image `local/portico-rs-build:3`, id `sha256:7fe216eb167da9e02e07eb460c98d2686a90e24e0850c9b78170c894ab21cff9`),
`rustc 1.99.1 (58cf8f9c3 2026-10-13)`, `SOURCE_DATE_EPOCH=1794577981`, 4 cores, each run about 4.5 minutes per architecture on a warm cargo home.
Repeat with `PORTICO_RS_REAL_BUILD=1` (see above). The sums depend on the pins above; a changed pin or source means a new result here.

Run 1:
```
e3e6f36ac79948dd557a054ab4519c7b0fc5f007f7e0cc382f6c721eb98cff9e  portico-transport-linux-amd64
410105985ff209003391b854b9f5fa4f7b67dd2c71c3fd4b8135433e41cb2be4  portico-transport-linux-arm64
```

Run 2:
```
e3e6f36ac79948dd557a054ab4519c7b0fc5f007f7e0cc382f6c721eb98cff9e  portico-transport-linux-amd64
410105985ff209003391b854b9f5fa4f7b67dd2c71c3fd4b8135433e41cb2be4  portico-transport-linux-arm64
```

## 2. Tags, versions and the wheel

One wheel name (`portico_transport`, distribution `portico-transport`), **two lineages, one version line**:

| Tag | Lineage | Version | Wheel |
| :--- | :--- | :--- | :--- |
| `transport-v<X.Y.Z>` | Go (existing family, last `transport-v0.6.10`) | 0.6.x, and the Go ROLLBACK re-release in the reserved line `0.9.x` (`transport-v0.9.0`) | `portico_transport-X.Y.Z-py3-none-linux_x86_64.whl` |
| `transport-rs-v<X.Y.Z>` | Rust (new) | 0.7.x; candidates `0.7.0rcN` (below `0.7.0` in PEP 440) | `portico_transport-X.Y.Z-py3-none-linux_{x86_64,aarch64}.whl` |

`transport-v*` does not match `transport-rs-v*`, so tooling that lists Go releases never sees a Rust one. The platform tags are the plain
`linux_x86_64` / `linux_aarch64` (static binaries; pip on a musl system such as Home Assistant OS refuses `manylinux2014_aarch64`,
which the old Go script used for arm64). Each wheel carries only its own binary and a `portico_transport/__init__.py` with the
the `binary_path()` the hub calls (its only call) plus `BACKEND = "rust"`.

**Both lineages can exist side by side as release assets, but only one can be installed:** same distribution name, so pip
replaces one with the other. Which one is installed is whichever wheel `door/wheel.py` installed last.

### How `door/wheel.py` selects, and the rollback

`async_ensure_wheel` reads the installed version and returns without any network call when `installed >= WHEEL_PIN_VERSION`;
otherwise it installs `WHEEL_REQUIREMENT` (a URL requirement, so the lineage is whatever that URL serves). Selecting a lineage is
therefore choosing the URL and a version the installed one is below. Consequences:

* **Upgrade Go to Rust**: pin `transport-rs-v0.7.0` (0.7.0 > 0.6.10): installs.
* **Repin back to Go 0.6.10 while Rust 0.7.x is installed: a no-op** (`0.7.0 >= 0.6.10`). The Rust binary keeps running. Test:
  `test_wheel.py::test_repinning_to_the_old_go_wheel_over_a_rust_wheel_does_nothing`.
* **Rolling back to the Go build is retired** (D-20261119-c1af, "we will not roll back to the Go wheel anymore"; cleanup D-20261123-cc1e).
  The machinery that re-released the Go build under a version above every Rust one (`release_scheme.go_rollback_version()`, the reserved
  `0.9.x` line, `release_go_rollback.py`, `build_go_binaries.sh`, `--go-binaries` of the dry run) is deleted, with the Go source it built.
  The old `transport-v*` tags and releases stay on the Gitea server; `release_scheme` still parses them, and `0.9.x` stays reserved so
  a Rust release never takes a number an old Go tag used. A rollback in future would be a Rust one: re-release the earlier Rust build
  under a higher version, because `door/wheel.py` installs only when installed < pin.
* `test_manifest.py` accepts both tag families now (`transport(-rs)?-v`); `WHEEL_PIN_URL` itself is untouched.

## 3. The dry run

```sh
export PORTICO_RELEASE_BASE_URL=<web root of the repository>                           # or --base-url; there is no default
packaging/rust/release_dryrun.py --version 0.7.0 --out /tmp/rel                     # cross-build, wheels, manifest
packaging/rust/release_dryrun.py --version 0.7.0 --out /tmp/rel --binaries /tmp/portico-rs-out    # from prebuilt, sealed binaries
```

The dry run refuses uncommitted changes in `transport-rs/` and `packaging/rust/` (`--allow-dirty` for a throwaway run). With `--binaries DIR` it requires `DIR/SHA256SUMS` and `DIR/build-info.txt` (written by the cross-build), verifies every sum, and refuses unless `commit` is HEAD and `transport-rs=clean`; both files are copied into the manifest (`binaries_sha256sums`, `binaries_build_info`). A fresh build is held to the same checks.

`manifest.json` lists every artefact (size, sha256, backend, arch, tag), the tag it WOULD create (`would_create_tag`), the URLs it would
upload to (`would_upload`), the tags already taken (a version is never reused), and the `wheel.py` lines a pin change would carry
(`pin_change_not_applied`, `applied: false`): `WHEEL_PIN_VERSION`, `WHEEL_PIN_URL` ending in `#sha256=<hex>` (pip verifies a hash fragment on a direct URL) and `WHEEL_PIN_SHA256`. `dry_run: true`, `uploaded`/`tag_created`/`pin_changed: false` are written literally. The script's git is an
allow-list of read-only subcommands (`tag` only as a listing); a test fails if publishing code (`git push`, `curl`, `urllib`, ...) appears in it.

The real release is a human step: create the Gitea release for the tag and upload the wheels, then a separate PR that edits the two
constants of `wheel.py` (and `hub.MIN_WHEEL_VERSION` only if the Python starts to rely on something the new wheel adds), merged on the maintainer's word.
The pin is per architecture since DEV-764 cut-over prep: `wheel.py` has `WHEEL_PIN_URLS` (`platform.machine()` to URL; `WHEEL_PIN_URL` stays
the x86_64 one) and the manifests print `WHEEL_PIN_URLS` with a hash per architecture. `test_manifest.py`, `test_house_hub.py`,
`test_packaging.py` and `scripts/build_test_venv.sh` accept both tag families (`transport-v`, `transport-rs-v`); the venv script reads the
current pin from `wheel.py` for the host architecture instead of a hardcoded Go URL.

## 4. The standalone image

`packaging/standalone/Dockerfile.rust` wraps the static binary with the defaults, ports, user and command line the former Go image had, so the compose snippet and
README apply unchanged. Build it with the cross-build's output directory as the context. Not deployed.

## What this does not cover

Everything live: publishing, the pin PR, an install on any Home Assistant, the dev-HA upgrade/downgrade rehearsal (spike S4), a restart, the
soak, G3 and retiring Go. The aarch64 binary was linked and inspected but not run (no qemu here).
