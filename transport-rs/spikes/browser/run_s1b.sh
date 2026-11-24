#!/bin/sh
# usage: run_s1b.sh <answerer-binary> <label> <firefox|chrome> [scenario ...]
# The scenario matrix of RESULTS_S1b.md. Results of each run land in $SPIKE_LOGDIR/<label>-<scenario>-<time>/.
# Each run's one-line summary is appended to $SPIKE_LOGDIR/<label>-<browser>.summary.
set -u
BIN=$1; LABEL=$2; BR=$3; shift 3
H=$(dirname "$0")/harness.py
CACHE=${S1B_CACHE:-/home/agent/.claude/jobs/adec9d89/tmp/cache}
OUT=${SPIKE_LOGDIR:-$CACHE/runs}
export SPIKE_LOGDIR=$OUT
SCEN=${*:-"A C1 C2 C3 D E E5 E4 E3 F H1 H2"}
run() { # name, extra args...
  name=$1; shift
  echo "== $LABEL $BR $name $(date +%T)" >> $OUT/$LABEL-$BR.summary
  python3 -I $H --answerer $BIN --browser $BR --label $LABEL-$BR "$@" > $OUT/$LABEL-$BR-$name.json 2>$OUT/$LABEL-$BR-$name.err
  python3 -I $(dirname "$0")/summarise.py $(ls -dt $OUT/$LABEL-$BR-*-* | head -1) >> $OUT/$LABEL-$BR.summary 2>&1
}
for s in $SCEN; do
  case $s in
    A)  run A  --scenario A --duration 60 ;;
    C1) run C1 --scenario C1 --duration 15 --mode candfirst ;;
    C2) run C2 --scenario C2 --duration 15 --mode normal --answerer-arg=--answer-delay-ms --answerer-arg=1500 ;;
    C3) run C3 --scenario C3 --duration 15 --mode candsdp ;;
    D)  run D  --scenario D --duration 45 --restart-at 20 ;;
    E)  run E  --scenario E --duration 300 --kill-at 15 --wait-after-kill 32 ;;
    E5) run E5 --scenario E5 --duration 300 --kill-at 15 --wait-after-kill 20 --answerer-arg=--max-stun-retransmits --answerer-arg=5 ;;
    E4) run E4 --scenario E4 --duration 300 --kill-at 15 --wait-after-kill 20 --answerer-arg=--max-stun-retransmits --answerer-arg=4 ;;
    E3) run E3 --scenario E3 --duration 300 --kill-at 15 --wait-after-kill 20 --answerer-arg=--max-stun-retransmits --answerer-arg=3 ;;
    F)  run F  --scenario F --duration 30 --browsers 2 ;;
    H1) if [ $BR = firefox ]; then run H1 --scenario H1 --duration 15 --obfuscate true; else run H1 --scenario H1 --duration 15 --src osc; fi ;;
    H2) if [ $BR = firefox ]; then run H2 --scenario H2 --duration 15 --obfuscate false; else run H2 --scenario H2 --duration 15 --chrome-flag=--disable-features=WebRtcHideLocalIpsWithMdns; fi ;;
  esac
done
echo ALLDONE >> $OUT/$LABEL-$BR.summary
