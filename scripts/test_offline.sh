#!/bin/sh
# Offline test runner for portico-ha (PH-0). Runs the Python suite in the two pytest
# sessions docs/development/running-tests.md describes, inside a throwaway container with no network and the
# read-only venv volume, so a PR can be verified on each HA version with one command.
#
#   scripts/test_offline.sh custom_components/portico
#   VENV=<other-volume> scripts/test_offline.sh custom_components/portico
#   scripts/test_offline.sh --node        # card/panel node tests (see below)
#
# Arguments are component directories (each holding a tests/ directory): there is one, custom_components/portico
# (the door side is its door/ package; its tests live in the same tests/ directory). A directory with no tests/
# is skipped with a note. Extra pytest arguments: PYTEST_ARGS="-k name".
# Exit status is non-zero if any session fails.
#
# Session 2 (-p no:homeassistant -p no:socket) holds the files whose real Unix sockets the HA
# harness's socket blocking breaks; every other tests/test_*.py file is session 1.
#
# VENV volume (HA + pytest-homeassistant-custom-component, mounted read-only at /v):
#   portico-py314-venv-2610-2   homeassistant 2026.10.2 (the live version), pytest-homeassistant-custom-component
#                               0.13.373, Python 3.14 (python:3.14-slim); the default. HA 2026.9.4 / 2026.10.0
#                               are no longer tested (their volumes are gone).
# Besides HA and the plugin the venv needs three packages: qrcode 8.2 (portico's manifest requirement),
# PyTurboJPEG 1.8.3 (HA's camera component) and the portico_transport 0.6.10 wheel (x86_64, from the repo release;
# on the box the integration installs it itself, `door/wheel.py`, but the offline tests run with no network, so it is
# preinstalled here and must match WHEEL_PIN_VERSION).
#
# REBUILD (online, default docker bridge only; refuses an existing volume unless --force):
#   scripts/build_test_venv.sh portico-py314-venv-2610-2 2026.10.2     # a new HA: scripts/build_test_venv.sh <volume> <version>
#   scripts/build_test_venv.sh --node                                  # portico-www-nodemods (npm ci)
# WARNING: these volumes are NOT durable. They live only on the agent host (agent-host), are not
# backed up, and have vanished before (the earlier ones were removed; unused local volumes may be
# removed on that host). If this script says the volume is missing, rebuild it as above.
#
# --node: card/panel tests (custom_components/portico/www/tests) on node:22 with no network.
# Needs the volume portico-www-nodemods holding jsdom (scripts/build_test_venv.sh --node).
# Each file is bounded by NODE_TEST_TIMEOUT seconds (default 120): a test that hangs fails the run (DEV-720).
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VENV=${VENV:-portico-py314-venv-2610-2}
IMAGE=${IMAGE:-python:3.14-slim}
SESSION2="test_transport.py test_remote_link.py test_call_state.py test_lifecycle.py test_strings.py test_door_strings.py test_push_wire.py test_google_services.py test_unifi_camera.py test_unify_migration.py test_camera_stream_size_wire.py"

if [ "${1:-}" = "--node" ]; then
  docker volume inspect portico-www-nodemods >/dev/null 2>&1 || {
    echo "test_offline: volume portico-www-nodemods does not exist (volumes are not durable); rebuild it with: scripts/build_test_venv.sh --node" >&2; exit 2; }
  exec docker run --rm --network none -v portico-www-nodemods:/nm:ro -v "$ROOT":/src:ro node:22 sh -c '
    set -e
    # DEV-720: a test that prints its pass line and never exits (a live picture timer) once ran as an orphan
    # container for 7 days. Bound every file so a hang fails the run loudly (exit 124) instead.
    t() { timeout -k 5 "${NODE_TEST_TIMEOUT:-120}" node --no-experimental-global-navigator "$1" || { rc=$?; [ $rc -eq 124 ] && echo "test_offline: $1 hung past ${NODE_TEST_TIMEOUT:-120}s (never exited)" >&2; exit $rc; }; }
    cp -r /src/custom_components/portico/www /tmp/www
    ln -s /nm/w/node_modules /tmp/www/tests/node_modules
    cd /tmp/www/tests
    t test_card.mjs
    t test_card_content.mjs
    t test_card_screen.mjs
    t test_card_wake.mjs
    t test_card_kiosk_close.mjs
    t test_card_failsafe.mjs
    t test_wake_listener.mjs
    t test_card_wide.mjs
    t test_card_house.mjs
    t test_card_live.mjs
    t test_card_escape.mjs
    t test_card_overlay_tiles.mjs
    t test_panel_logic.mjs
    t test_panel_editor.mjs
    t test_panel_blocks.mjs
    t test_panel_camspec.mjs
    t test_panel_screens.mjs
    t test_panel_kiosk.mjs
    t test_panel_phones.mjs
    t test_panel_first_page.mjs
    t test_panel_leftover_devices.mjs
    t test_panel_settings.mjs
    t test_panel_live.mjs
    t test_elements_render.mjs
    t test_panel_elements.mjs
    t test_panel_camrefs.mjs
    t test_panel_buttonrefs.mjs
    t test_panel_group_call.mjs
    t test_panel_pages_unified.mjs
    t test_panel_pages_sections.mjs
    t test_panel_devices_selectors.mjs
    t test_panel_make_panel.mjs
    t test_panel_own_pages.mjs
    t test_panel_config_banner.mjs'
fi

[ $# -gt 0 ] || { echo "usage: VENV=<volume> $0 <part-dir>... | $0 --node" >&2; exit 2; }

docker volume inspect "$VENV" >/dev/null 2>&1 || {
  echo "test_offline: venv volume $VENV does not exist (volumes are not durable). Build it with:" >&2
  echo "  scripts/build_test_venv.sh $VENV <homeassistant-version>     e.g. 2026.10.2" >&2
  exit 2; }

run() { # run <extra pytest flags> <files...>
  extra=$1; shift
  # shellcheck disable=SC2086
  docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$VENV":/v:ro -v "$ROOT":/src \
    -w /src "$IMAGE" /v/bin/python -m pytest -q -p no:cacheprovider -o asyncio_mode=auto \
    $extra ${PYTEST_ARGS:-} "$@"
}

rc=0
for part in "$@"; do
  part=${part%/}
  t=$part/tests
  if [ ! -d "$ROOT/$t" ]; then echo "== $part: no tests/ directory, skipped"; continue; fi
  s1=""; s2=""
  for f in $(cd "$ROOT/$t" && ls test_*.py); do
    case " $SESSION2 " in *" $f "*) s2="$s2 $t/$f";; *) s1="$s1 $t/$f";; esac
  done
  echo "== $part, session 1 (HA harness), venv $VENV"
  # shellcheck disable=SC2086
  run "" $s1 || rc=1
  if [ -n "$s2" ]; then
    echo "== $part, session 2 (-p no:homeassistant -p no:socket), venv $VENV"
    # shellcheck disable=SC2086
    run "-p no:homeassistant -p no:socket" $s2 || rc=1
  fi
done
exit $rc
