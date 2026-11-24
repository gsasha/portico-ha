#!/bin/sh
# The shipped-binary gate (R2-35, R3B-2): a release build carries no test-seam injection text.
#   scripts/shipped_gate.sh [BINARY]        default: target/release/portico-transport (cargo build --release first)
# It is the RELEASE binary that ships, and in it the dead `if flag { panic!("injected ..") }` branches that are
# not `cfg`-gated are removed by the optimiser; a debug build without the feature keeps them, so a debug binary
# is NOT a valid argument (it prints a non-zero count by design). Prints the count and exits 1 when it is not 0.
bin=${1:-target/release/portico-transport}
[ -f "$bin" ] || { echo "no such binary: $bin (cargo build --release first)" >&2; exit 2; }
n=$(grep -a -o -e 'injected' -e 'test panic' -e 'SECRET-API-KEY' "$bin" | wc -l)
echo "$n"
[ "$n" -eq 0 ]
