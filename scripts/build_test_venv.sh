#!/bin/sh
# Rebuild a test-venv volume (or the node_modules volume) that scripts/test_offline.sh mounts.
#
#   scripts/build_test_venv.sh <volume-name> <homeassistant-version> [--wheel <path>] [--force]
#   scripts/build_test_venv.sh --node [--force]        # (re)build portico-www-nodemods (npm ci)
#
#   scripts/build_test_venv.sh portico-py314-venv-2610-2 2026.10.2
#   VENV=portico-py314-venv-2610-2 scripts/test_offline.sh custom_components/portico
#
# What goes into the volume (python:3.14-slim, a plain venv at the volume root, mounted at /v):
#   homeassistant==<version>
#   pytest-homeassistant-custom-component  the release that pins exactly that homeassistant
#                                          (looked up on PyPI; override with PLUGIN_VERSION=0.13.373)
#   qrcode==8.2       portico's manifest requirement (qrcode>=7.4; HA would install it at runtime, tests run offline)
#   PyTurboJPEG==1.8.3 needed by HA's camera component
#   portico_transport the platform wheel from the repo release. Default: the CURRENT PIN, read from WHEEL_PIN_URLS in
#                     custom_components/portico/door/wheel.py (the integration is one; the pin is a constant there, not
#                     a manifest requirement), for this host's architecture (`uname -m`; x86_64 when the pin has no
#                     entry for it). It follows the pin through a tag-family change (Go `transport-v<ver>` to Rust
#                     `transport-rs-v<ver>`) without an edit here. Override with $WHEEL_URL (a full URL) or a local
#                     file with --wheel <path>.
#
# Safety: refuses to touch an existing volume unless --force (then the volume is removed and
# recreated, by that exact name only). The pip/npm step runs on Docker's DEFAULT bridge network,
# never a user network, and the script refuses to run if that bridge sits in 10.0.0.0/8 (estate
# address space). The test runs themselves use --network none.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
# The pinned wheel URL for this host's architecture, read (not imported: wheel.py needs Home Assistant) from the literal
# WHEEL_PIN_URLS dict in door/wheel.py. Prints nothing and fails when the dict is missing or unparsable.
pin_wheel_url() {
  python3 - "$ROOT/custom_components/portico/door/wheel.py" "$(uname -m)" <<'PY'
import ast, sys
path, machine = sys.argv[1], sys.argv[2]
for node in ast.parse(open(path).read()).body:
    if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "WHEEL_PIN_URLS" for t in node.targets):
        urls = ast.literal_eval(node.value)
        print(urls.get(machine) or urls["x86_64"])
        sys.exit(0)
sys.exit(1)
PY
}
if [ -z "${WHEEL_URL:-}" ]; then
  WHEEL_URL=$(pin_wheel_url) || { echo "cannot read WHEEL_PIN_URLS from door/wheel.py; set WHEEL_URL" >&2; exit 1; }
  # WHEEL_BASE_URL: fetch the same release files from another web root (the wheel_base_url option of the integration).
  if [ -n "${WHEEL_BASE_URL:-}" ]; then
    WHEEL_URL="${WHEEL_BASE_URL%/}${WHEEL_URL#https://github.com/gsasha/portico-ha}"
  fi
fi
IMAGE=${IMAGE:-python:3.14-slim}
NODE_IMAGE=${NODE_IMAGE:-node:22}

usage() {
  echo "usage: $0 <volume-name> <homeassistant-version> [--wheel <path>] [--force]" >&2
  echo "       $0 --node [--force]" >&2
  exit 2
}

force=0; wheel=""; node=0; pos=""
while [ $# -gt 0 ]; do
  case $1 in
    --force) force=1 ;;
    --node) node=1 ;;
    --wheel) [ $# -ge 2 ] || usage; wheel=$2; shift ;;
    -h|--help) usage ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) pos="$pos $1" ;;
  esac
  shift
done
# shellcheck disable=SC2086
set -- $pos

# The pip/npm step needs the internet, so it may only use the default bridge, and that bridge must
# not overlap the estate (10.0.0.0/8).
bridge=$(docker network inspect bridge --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}') \
  || { echo "cannot inspect the docker bridge network" >&2; exit 1; }
case " $bridge" in
  *" 10."*) echo "refusing: the default docker bridge is $bridge (inside 10.0.0.0/8)" >&2; exit 1 ;;
esac

# claim <volume>: create it, or refuse (or, with --force, recreate it) when it exists.
claim() {
  if docker volume inspect "$1" >/dev/null 2>&1; then
    if [ "$force" -ne 1 ]; then
      echo "volume $1 already exists; refusing to touch it (use --force to remove and rebuild it)" >&2
      exit 1
    fi
    echo "== --force: removing volume $1"
    docker volume rm "$1" >/dev/null
  fi
  docker volume create "$1" >/dev/null
}

if [ "$node" -eq 1 ]; then
  [ $# -eq 0 ] || usage
  vol=portico-www-nodemods
  claim "$vol"
  echo "== npm ci into $vol"
  docker run --rm --network bridge -v "$vol":/nm -v "$ROOT/custom_components/portico/www/tests":/t:ro "$NODE_IMAGE" sh -c \
    'mkdir -p /nm/w && cp /t/package.json /t/package-lock.json /nm/w/ && cd /nm/w && npm ci'
  echo "== done: $vol  (scripts/test_offline.sh --node)"
  exit 0
fi

[ $# -eq 2 ] || usage
vol=$1; ha=$2
case $vol in *[!A-Za-z0-9_.-]*|"") echo "bad volume name: $vol" >&2; exit 2 ;; esac
case $ha in *[!0-9.]*|"") echo "bad homeassistant version: $ha" >&2; exit 2 ;; esac

wheel_mount=""; wheel_arg=$WHEEL_URL
if [ -n "$wheel" ]; then
  [ -f "$wheel" ] || { echo "no such wheel file: $wheel" >&2; exit 2; }
  wdir=$(cd "$(dirname "$wheel")" && pwd)
  wheel_mount="-v $wdir:/wheel:ro"
  wheel_arg=/wheel/$(basename "$wheel")
fi

claim "$vol"
echo "== building $vol: homeassistant $ha on $IMAGE"
# The inner script runs as one sh -c; it reads HA / WHEEL / PLUGIN_VERSION from the environment.
# shellcheck disable=SC2086
docker run --rm --network bridge -e HA="$ha" -e WHEEL="$wheel_arg" -e PLUGIN_VERSION="${PLUGIN_VERSION:-}" \
  -v "$vol":/v $wheel_mount "$IMAGE" sh -ec '
    python -m venv /v
    /v/bin/python -m pip install -q --upgrade pip
    if [ -z "$PLUGIN_VERSION" ]; then
      PLUGIN_VERSION=$(python - <<PY
import json, re, urllib.request
def get(u):
    return json.load(urllib.request.urlopen(u, timeout=60))
name = "pytest-homeassistant-custom-component"
rels = get(f"https://pypi.org/pypi/{name}/json")["releases"]
def key(v):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.]", v))
for v in sorted(rels, key=key, reverse=True):
    if not rels[v] or any(f.get("yanked") for f in rels[v]):
        continue
    reqs = get(f"https://pypi.org/pypi/{name}/{v}/json")["info"].get("requires_dist") or []
    if any(r.replace(" ", "").startswith("homeassistant==" + "$HA" + ";") or r.replace(" ", "") == "homeassistant==$HA" for r in reqs):
        print(v)
        break
PY
)
      [ -n "$PLUGIN_VERSION" ] || { echo "no pytest-homeassistant-custom-component release pins homeassistant==$HA" >&2; exit 3; }
    fi
    echo "== plugin: pytest-homeassistant-custom-component==$PLUGIN_VERSION"
    /v/bin/python -m pip install -q "homeassistant==$HA" "pytest-homeassistant-custom-component==$PLUGIN_VERSION" \
      "qrcode==8.2" "PyTurboJPEG==1.8.3" "$WHEEL"
    /v/bin/python -m pip list 2>/dev/null | grep -i -E "^(homeassistant|pytest-homeassistant|qrcode|PyTurboJPEG|portico)"
  '
echo "== done: $vol   VENV=$vol scripts/test_offline.sh custom_components/portico"
