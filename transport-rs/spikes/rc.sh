#!/bin/sh
# usage: rc.sh <crate-dir> <cargo args...>   -- run cargo in rust:1-bookworm, as the current user
d="$1"; shift
cd "$(dirname "$0")"
mkdir -p .cargo-home
exec docker run --rm -v "$PWD:/src" -w "/src/$d" -e CARGO_HOME=/src/.cargo-home -u "$(id -u):$(id -g)" ${RC_ENV} rust:1-bookworm cargo "$@"
