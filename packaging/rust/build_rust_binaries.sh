#!/bin/sh
# Reproducible static (musl) cross-build of the Rust transport for x86_64 and aarch64 (DEV-764 Phase 4).
#
#   packaging/rust/build_rust_binaries.sh [--arch x86_64|aarch64|all] [--fetch-only] [--no-fetch] OUT_DIR
#
# Writes OUT_DIR/portico-transport-linux-amd64, OUT_DIR/portico-transport-linux-arm64 (the file names the Go wheels
# use, so `portico_transport.binary_path()` is the same function for both), OUT_DIR/SHA256SUMS and OUT_DIR/build-info.txt.
# Nothing is published, tagged or installed; OUT_DIR and the caches are the only things written.
#
# Offline-capable: the build image (Dockerfile.build) is built once and the crate sources are fetched once
# (`cargo fetch --locked`, the ONLY step with network). The compile itself always runs `--network none --offline`
# with the source mounted read-only, so a cold cache fails loudly instead of silently reaching out. With
# --no-fetch the fetch step is skipped (use it when the cargo home is already warm and the host is offline).
#
# Reproducibility knobs: --locked (Cargo.lock is the truth), CARGO_INCREMENTAL=0, SOURCE_DATE_EPOCH = the commit time,
# `--remap-path-prefix` for the source, cargo home and target dir, strip + LTO from [profile.release]. To check:
# run twice into two directories and compare the two SHA256SUMS files (packaging/rust/README.md shows the result).
#
# Environment (all optional):
#   PORTICO_RS_CACHE      cache root, default ${XDG_CACHE_HOME:-$HOME/.cache}/portico-rs  (cargo/, target/, home/)
#   PORTICO_RS_CARGO_HOME / PORTICO_RS_TARGET_DIR   override the two big directories separately
#   PORTICO_RS_IMAGE      build image tag, default local/portico-rs-build:3 (built on first use)
#   DOCKER                the docker binary (the tests substitute a stub), default docker
#   SOURCE_DATE_EPOCH     default: the HEAD commit time
set -eu

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
crate="$repo/transport-rs"
DOCKER=${DOCKER:-docker}
IMAGE=${PORTICO_RS_IMAGE:-local/portico-rs-build:3}
cache=${PORTICO_RS_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/portico-rs}
cargo_home=${PORTICO_RS_CARGO_HOME:-$cache/cargo}
target_dir=${PORTICO_RS_TARGET_DIR:-$cache/target}
home_dir=$cache/home

arch=all; fetch=1; fetch_only=0; out=
while [ $# -gt 0 ]; do
  case $1 in
    --arch) arch=${2:?--arch needs a value}; shift 2 ;;
    --fetch-only) fetch_only=1; shift ;;
    --no-fetch) fetch=0; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) out=$1; shift ;;
  esac
done
case $arch in x86_64|aarch64|all) ;; *) echo "--arch must be x86_64, aarch64 or all" >&2; exit 2 ;; esac
[ -n "$out" ] || [ "$fetch_only" = 1 ] || { echo "usage: $0 [--arch ...] [--fetch-only] [--no-fetch] OUT_DIR" >&2; exit 2; }

mkdir -p "$cargo_home" "$target_dir" "$home_dir"
cargo_home=$(cd "$cargo_home" && pwd); target_dir=$(cd "$target_dir" && pwd); home_dir=$(cd "$home_dir" && pwd)

if ! "$DOCKER" image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "== building the build image $IMAGE (online, once)"
  "$DOCKER" build -f "$here/Dockerfile.build" -t "$IMAGE" "$here"
fi

# Same user inside as outside: nothing root-owned is left behind in the caches or OUT_DIR.
run() { # run <network> <extra docker args and the command...>
  net=$1; shift
  "$DOCKER" run --rm --network "$net" -u "$(id -u):$(id -g)" \
    -v "$crate:/work:ro" -v "$cargo_home:/cache/cargo" -v "$target_dir:/cache/target" -v "$home_dir:/cache/home" \
    -w /work -e HOME=/cache/home -e CARGO_HOME=/cache/cargo -e CARGO_TARGET_DIR=/cache/target -e CARGO_INCREMENTAL=0 \
    "$@"
}

if [ "$fetch" = 1 ]; then
  echo "== fetching crate sources (the only step with network)"
  run bridge "$IMAGE" cargo fetch --locked --target x86_64-unknown-linux-musl --target aarch64-unknown-linux-musl
fi
[ "$fetch_only" = 0 ] || { echo "fetch done"; exit 0; }

epoch=${SOURCE_DATE_EPOCH:-$(git -C "$repo" log -1 --format=%ct)}
commit=$(git -C "$repo" rev-parse HEAD)
dirty=clean; [ -z "$(git -C "$repo" status --porcelain -- transport-rs)" ] || dirty=DIRTY
mkdir -p "$out"; out=$(cd "$out" && pwd)

flags="--remap-path-prefix=/work=/src --remap-path-prefix=/cache/cargo=/cargo --remap-path-prefix=/cache/target=/target"
# The x86_64 linker is deliberately NOT overridden: rustc's self-contained musl linking through the image's `cc` gives a
# truly static binary. Pointing the linker at musl-gcc produced a static-pie WITH a PT_INTERP naming
# /lib/ld-musl-x86_64.so.1, which does not run on a host without that file. aarch64 needs its cross gcc as the linker.
build_one() { # build_one <rust-triple> <wheel-arch-name> <cc> <ar> <linker or "">
  triple=$1; name=portico-transport-linux-$2
  echo "== building $triple"
  under=$(echo "$triple" | tr '-' '_'); up=$(echo "$under" | tr 'a-z' 'A-Z')
  linker=; [ -z "$5" ] || linker="CARGO_TARGET_${up}_LINKER=$5"
  run none -e SOURCE_DATE_EPOCH="$epoch" -e RUSTFLAGS="$flags" \
    -e "CC_$under=$3" -e "AR_$under=$4" ${linker:+-e "$linker"} \
    "$IMAGE" cargo build --release --locked --offline --target "$triple"
  # A static binary has no program interpreter and no shared-library needs (see the linker note above). readelf runs INSIDE
  # the build image (binutils is there), so the check does not depend on the host; a non-zero exit (not an ELF, missing
  # file) fails the build instead of passing it.
  bin=/cache/target/$triple/release/portico-transport
  if ! phdrs=$(run none "$IMAGE" readelf -lW "$bin") || ! dyn=$(run none "$IMAGE" readelf -d "$bin"); then
    echo "ERROR: readelf could not read $triple/release/portico-transport (not an ELF file?)" >&2; exit 1
  fi
  if printf '%s\n' "$phdrs" | grep -q INTERP || printf '%s\n' "$dyn" | grep -q NEEDED; then
    echo "ERROR: $name is not fully static (PT_INTERP or NEEDED present)" >&2; exit 1
  fi
  cp "$target_dir/$triple/release/portico-transport" "$out/$name"
}
want() { [ "$arch" = all ] || [ "$arch" = "$1" ]; }
want x86_64  && build_one x86_64-unknown-linux-musl  amd64 musl-gcc ar ""
want aarch64 && build_one aarch64-unknown-linux-musl arm64 aarch64-linux-gnu-gcc aarch64-linux-gnu-ar aarch64-linux-gnu-gcc

( cd "$out" && sha256sum portico-transport-linux-* > SHA256SUMS )
{
  echo "commit=$commit"
  echo "transport-rs=$dirty"
  echo "source_date_epoch=$epoch"
  echo "image=$IMAGE"
  echo "image_id=$("$DOCKER" image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null || echo unknown)"
  echo "rustc=$(run none "$IMAGE" rustc --version 2>/dev/null || echo unknown)"
} > "$out/build-info.txt"
echo "== done"; cat "$out/SHA256SUMS"
