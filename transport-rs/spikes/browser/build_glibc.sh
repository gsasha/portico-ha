#!/bin/sh
# usage: build_glibc.sh <crate-dir> <cargo args...>      e.g.  build_glibc.sh answerer-str0m build --release --bin prod
# glibc build in rust:1-bookworm (the host is Debian 12, so the binary runs on it); ALL caches (cargo home, target dir)
# live OUTSIDE the worktree under $S1B_CACHE, so nothing read-only is left in the worktree. TGT picks the target dir name.
C=${S1B_CACHE:-/home/agent/.claude/jobs/adec9d89/tmp/cache}
W=$(cd "$(dirname "$0")/.." && pwd)
crate=$1; shift
mkdir -p "$C/cargo" "$C/target" "$C/home"
exec docker run --rm --cpus "${CPUS:-1}" -u "$(id -u):$(id -g)" -v "$C:/cache" -v "$W:/src" -w "/src/$crate" \
  -e CARGO_HOME=/cache/cargo -e CARGO_TARGET_DIR=/cache/target/${TGT:-$crate} -e HOME=/cache/home rust:1-bookworm cargo "$@"
