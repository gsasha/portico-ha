# Running the tests

How to run the Python and Node tests, offline in a throwaway container, and what the contract and push tests record.

Note: `custom_components/portico/tests/test_strings.py` reads the documentation under `docs/` (the Tailscale guide, the stored-secret wording) and fails when a required sentence is moved or removed. See [Tailscale](../guides/tailscale.md).

## Quick start

From the repository root, in a throwaway Docker container with no network:

```
scripts/build_test_venv.sh portico-py314-venv-2610-2 2026.10.2   # once: builds the venv volume (online)
scripts/test_offline.sh custom_components/portico
scripts/test_offline.sh --node                                    # the card and panel (Node) tests
```

The full detail is under "Detail" below.

## Detail

Python tests run in two pytest sessions (pytest-homeassistant-custom-component's
socket blocking breaks the real-Unix-socket tests, see test_transport.py), from the repo
root, against `homeassistant==2026.9.4` + `pytest-homeassistant-custom-component` (the suite is
also green on `homeassistant==2026.10.0`; tests that remove a phone user request the
`hass_owner_user` fixture, because 2026.10 refuses to remove the owner):

```
python -m pytest -o asyncio_mode=auto <every test_*.py of custom_components/portico/tests/ except the ones below>
python -m pytest -p no:homeassistant -p no:socket -o asyncio_mode=auto \
    custom_components/portico/tests/test_transport.py custom_components/portico/tests/test_call_state.py \
    custom_components/portico/tests/test_lifecycle.py custom_components/portico/tests/test_unifi_camera.py \
    custom_components/portico/tests/test_strings.py custom_components/portico/tests/test_door_strings.py \
    custom_components/portico/tests/test_push_wire.py custom_components/portico/tests/test_google_services.py \
    custom_components/portico/tests/test_camera_stream_size_wire.py
```

Offline, in a throwaway container: one command, `scripts/test_offline.sh`. It runs both
pytest sessions above in a throwaway `python:3.14-slim` container with `--network none` and a
read-only venv volume, one part directory per argument (a part without a `tests/` directory is
skipped), and exits non-zero if either session fails. Every test file under `tests/` is picked up
by name; the files that need real Unix sockets (`test_transport.py`, `test_call_state.py`,
`test_lifecycle.py`, `test_strings.py`, `test_push_wire.py`, `test_google_services.py`,
`test_unifi_camera.py`, `test_door_strings.py`, `test_camera_stream_size_wire.py`) go to the second session, the rest to the first. (All of them
are in `portico/tests/`: the tests of the former `portico_intercom` moved there with the fold.) The hand-written file
lists above are the short form of what the script does.

```
# the live HA version, 2026.10.2 (default volume portico-py314-venv-2610-2)
scripts/test_offline.sh custom_components/portico
# another volume (built for another HA version)
VENV=<volume> scripts/test_offline.sh custom_components/portico
# the card and panel node tests (volume portico-www-nodemods holds jsdom)
scripts/test_offline.sh --node
```

`PYTEST_ARGS="-k name"` passes extra arguments to pytest. We test one Home Assistant version
(2026.10.2); 2026.9.4 and 2026.10.0 are no longer needed. The venv holds `homeassistant`, the
`pytest-homeassistant-custom-component` release that pins it (0.13.373 for 2026.10.2), and three
packages that are easy to forget: `qrcode` 8.2 (this integration's manifest requirement),
`PyTurboJPEG` 1.8.3 (HA's camera component) and the `portico_transport` wheel (the version the pin in `door/wheel.py` names, 0.7.3 at the time of writing, for the machine's architecture;
`build_test_venv.sh` downloads it from the pin URL, so it needs that host to be reachable, or `--wheel <path>`). Create or rebuild a volume with one command, online, on Docker's default
bridge network only:

```
scripts/build_test_venv.sh portico-py314-venv-2610-2 2026.10.2   # [--wheel <path>] [--force]
scripts/build_test_venv.sh --node                                  # portico-www-nodemods (npm ci)
```

It picks the matching plugin release itself, and refuses an existing volume unless `--force` is
given. `test_offline.sh` stops early with that same hint when its volume is missing.

**The volumes are not durable.** They exist only on the machine that ran the build, are not backed
up, and have vanished before. The weekly `docker-prune` job never touches volumes (it removes only old
images and build cache), so the likely cause is an unused-volume removal on that host (`docker volume
rm` / `prune` of volumes no container references, which a bare test-venv volume is), and no label
exempts a volume. Treat a missing volume as normal and rebuild it. There is no CI service: quote the
run in a pull request.

**The push tests never reach Google.** `tests/fake_google.py` runs one local aiohttp server on
127.0.0.1 that stands in for both the OAuth token endpoint (it verifies the RS256 JWT against a
throwaway RSA key generated per run) and FCM `messages:send`. The tests point the code at it by
replacing `push/const.py`'s `ENDPOINTS`, the single override point; production code never assigns
it and defaults to the real Google URLs, to which the service account's `token_uri` is pinned.
The HA-harness tests that use it opt in to sockets with the harness's `socket_enabled` fixture
(connects stay limited to 127.0.0.1).

**The contract file.** `contract/push_v2.json` lists cases of FCM data messages as the phone
receives them and the expected reaction (`ring`, `silent`, `dismiss`, `missed`, and `quiet`: the
`kind: "test"` message, one quiet notification). The wire version every message carries (`pv`) is `"2"`:
v1's fields with the door ref derived from the door's endpoint id (`d-` and 32 hex, never an entry id or a URL),
plus two optional keys reserved for the house calls of M7, `ct` (`door` when absent, or `house`) and `from_ref`.
The sender sends neither until M7, and the app ignores `ct: house`. `test_push_wire.py` records the file from the
builders in `push/payload.py` (regenerate with `PORTICO_REGENERATE_WS_CONTRACT=1`), checks the expectations
are coherent, and pins the file's sha256. portico-app vendors the file with the same pinned sha256 and runs it
against its own decision function; any change is a version bump on both sides.

`test_push_integration.py` drives a fake call through the whole path (fake transport -> the call
tracker -> the notifier -> the fake Google) and asserts the exact ring/answered/ended requests: the
"no regression" check for any change to the sender.

**The WebSocket contract.** `contract/ws_v3.json` records, case by case, the requests a client sends
and every frame HA sends back, for every `portico/*` command and every kind of caller (an enrolled phone, a declared
screen, an administrator, an ordinary user), including each error code. `test_ws_contract.py` produces it from the
real handlers over a real HA websocket connection, with a fake door provider behind them, and fails when the
checked-in file differs. After an intended change, regenerate it with `PORTICO_REGENERATE_WS_CONTRACT=1` (in the
container, add `-e PORTICO_REGENERATE_WS_CONTRACT=1 --user "$(id -u):$(id -g)"` so the file is not written as root),
review the diff, and tell portico-app, whose MockWebServer tests replay it. The file is contract 3 (M6 E3):
`portico/version` returns `{contract: 3, ui_version, providers}`; the app sends it first and refuses any other
`contract`, retrying on its backoff. Each command's authorization class (`admin`, `phone`, `dashboard`, `any`) is in
the file's `_meta.auth`, generated from `auth.py`: one table, every handler calls `auth.require` first, and
`test_auth_table.py` fails when a registered command is missing from it. `contract/app_commands_v3.txt` lists the
commands the app sends; `test_fixture_covers_every_command_in_the_app_list` fails when one has no case in the file.
Contract 3 has no aliases: contract 2's `portico/devices`, `portico/subscribe` and `portico/content/list` are gone
(`unknown_command`), `device_id` is `door_id`, and `content/get` has no `profile` and no flat fields. The
card, panel and app of contract 2 (app 0.4.x) are no longer supported: the cutover to contract 3 is done (see
`portico_editor_design.md`), and the server refuses a push registration from an app older than 0.5.0.

The card's protocol tests (Node 21 or later is fine): `cd custom_components/portico/www/tests && npm ci && node test_card.mjs && node test_card_content.mjs && node test_card_screen.mjs && node test_card_escape.mjs`
(Node 20; Node 22's read-only global `navigator` breaks the jsdom shim at line 10).
