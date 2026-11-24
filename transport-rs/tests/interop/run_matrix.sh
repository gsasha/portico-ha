#!/bin/sh
# usage: run_matrix.sh <transport-binary> <firefox|chrome> [scenario ...]
# Runs the scenarios of harness.py one after the other against the REAL binary and appends one line per
# scenario to $INTEROP_OUT/<browser>.summary. Scenario G (hostile offers) needs no browser: hostile.py.
# Caches and run logs go to $INTEROP_OUT (default a temp dir), never into the worktree.
set -u
BIN=$1; BR=$2; shift 2
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${INTEROP_OUT:-$(mktemp -d)}
export INTEROP_OUT=$OUT
SCEN=${*:-"A B C D E F H1 H2 H3"}
mkdir -p "$OUT"
for s in $SCEN; do
  echo "== $BR $s $(date +%T)" >> "$OUT/$BR.summary"
  python3 -I "$HERE/harness.py" --binary "$BIN" --browser "$BR" --scenario "$s" > "$OUT/$BR-$s.json" 2> "$OUT/$BR-$s.err"
  echo "exit $?" >> "$OUT/$BR.summary"
  python3 -I -c 'import json,sys; v=json.load(open(sys.argv[1]))["verdict"]; print("passed", len(v["passed"]), "failed", len(v["failed"])); [print("  FAILED:", f) for f in v["failed"]]' "$OUT/$BR-$s.json" >> "$OUT/$BR.summary" 2>&1
done
echo ALLDONE >> "$OUT/$BR.summary"
