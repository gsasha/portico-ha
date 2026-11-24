#!/bin/sh
# usage: build_musl.sh <crate-dir> <tag> [extra cargo args...]
# Static release builds for x86_64-unknown-linux-musl and aarch64-unknown-linux-musl in rust:1-bookworm.
# Output: bin/<tag>-x86_64 and bin/<tag>-aarch64. x86_64 uses musl-tools (musl-gcc); aarch64 compiles C
# with gcc-aarch64-linux-gnu and links with it while rustc supplies the self-contained musl libc.
set -e
crate=$1; tag=$2; shift 2
cd "$(dirname "$0")"
mkdir -p bin .cargo-home
docker run --rm -v "$PWD:/src" -w "/src/$crate" -e CARGO_HOME=/src/.cargo-home \
  -e UIDGID="$(id -u):$(id -g)" -e CRATE="$crate" -e TAG="$tag" -e EXTRA="$*" rust:1-bookworm sh -c '
set -e
apt-get update -qq >/dev/null && apt-get install -y -qq musl-tools gcc-aarch64-linux-gnu >/dev/null
rustup target add x86_64-unknown-linux-musl aarch64-unknown-linux-musl 2>&1 | tail -1
export CC_x86_64_unknown_linux_musl=musl-gcc
export CC_aarch64_unknown_linux_musl=aarch64-linux-gnu-gcc AR_aarch64_unknown_linux_musl=aarch64-linux-gnu-ar
export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_LINKER=aarch64-linux-gnu-gcc
rc=0
for t in x86_64-unknown-linux-musl aarch64-unknown-linux-musl; do
  short=${t%%-*}
  if cargo build --release --target $t $EXTRA; then cp target/$t/release/$CRATE /src/bin/$TAG-$short; else echo "BUILD FAILED $t"; rc=1; fi
done
chown -R $UIDGID /src/.cargo-home /src/$CRATE/target /src/bin
exit $rc
'
