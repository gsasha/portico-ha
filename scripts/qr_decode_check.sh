#!/bin/sh
# Does the enrolment QR the panel shows READ? Runs custom_components/portico/tests/test_qr_decode.py with a REAL decoder:
# ZXing core 3.5.3 (the version portico-app's QrFrameDecoder uses) in a JDK, offline.
#
# A documented one-off, not part of test_offline.sh: no QR decoder wheel for Python 3.14 is in the test venv, and the
# venvs run with no network. Without PORTICO_QR_DECODER the decoding test is skipped (the structural one still runs).
#
#   JDK_DIR=<a JDK 17 home>  ZXING_JAR=<core-3.5.3.jar>  scripts/qr_decode_check.sh
#   VENV=<other-volume>  PORTICO_QR_DUMP_DIR=<dir to keep the PNGs>   (optional)
#
# JDK_DIR: for instance copy one out of an image you already have, offline:
#     docker create --name jdk gradle:8-jdk17 && docker cp jdk:/opt/java/openjdk ./jdk && docker rm jdk
# ZXING_JAR: com.google.zxing:core:3.5.3 (the Gradle cache of the app build has it).
# Every container runs with --network none.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VENV=${VENV:-portico-py314-venv-2610-2}
IMAGE=${IMAGE:-python:3.14-slim}
: "${JDK_DIR:?set JDK_DIR to a JDK 17 home}"
: "${ZXING_JAR:?set ZXING_JAR to core-3.5.3.jar}"

WORK=$(mktemp -d)
mkdir -p "$WORK/zx" "$WORK/out"
cp "$ZXING_JAR" "$WORK/zx/core.jar"
chmod -R a+rwX "$WORK"

# 1. compile the decoder
docker run --rm --network none -v "$JDK_DIR":/jdk:ro -v "$WORK":/work -v "$ROOT/scripts/zxing":/src:ro "$IMAGE" \
  /jdk/bin/javac -cp /work/zx/core.jar -d /work/out /src/Dec.java

# 2. run the tests with the decoder wired in (the JDK is mounted into the Python container)
DUMP_ARGS=""
if [ -n "${PORTICO_QR_DUMP_DIR:-}" ]; then
  mkdir -p "$PORTICO_QR_DUMP_DIR"
  DUMP_ARGS="-v $PORTICO_QR_DUMP_DIR:/dump -e PORTICO_QR_DUMP_DIR=/dump"
fi
# shellcheck disable=SC2086
docker run --rm --network none --user "$(id -u):$(id -g)" -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$VENV":/v:ro -v "$ROOT":/src:ro -v "$JDK_DIR":/jdk:ro -v "$WORK":/work:ro $DUMP_ARGS \
  -e PORTICO_QR_DECODER="/jdk/bin/java -Dfile.encoding=UTF-8 -cp /work/zx/core.jar:/work/out Dec" \
  -w /src "$IMAGE" /v/bin/python -m pytest -q -p no:cacheprovider -o asyncio_mode=auto -s -rs \
  custom_components/portico/tests/test_qr_decode.py
