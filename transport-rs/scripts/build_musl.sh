#!/bin/sh
# Superseded (R1-65): the locked, offline, read-only, rootless cross-build is
# ../../packaging/rust/build_rust_binaries.sh. This wrapper only forwards to it.
#
#   scripts/build_musl.sh [--arch x86_64|aarch64|all] OUT_DIR
exec "$(cd "$(dirname "$0")/../.." && pwd)/packaging/rust/build_rust_binaries.sh" "$@"
