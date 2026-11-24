#!/bin/sh
# Runs the S3 check in rust:1-bookworm; ffmpeg is NOT in that image nor on the host, so it is apt-installed inside the throwaway container.
cd "$(dirname "$0")/.."
mkdir -p .cargo-home
docker run --rm -v "$PWD:/src" -w /src/s3-ffmpeg -e CARGO_HOME=/src/.cargo-home -e CARGO_TARGET_DIR=/tmp/s3t -e UIDGID="$(id -u):$(id -g)" rust:1-bookworm sh -c '
apt-get update -qq >/dev/null && apt-get install -y -qq ffmpeg >/dev/null 2>&1; ffmpeg -version | head -1
cargo run --release 2>&1 | tail -3; chown -R $UIDGID /src/.cargo-home /src/s3-ffmpeg 2>/dev/null; true'
