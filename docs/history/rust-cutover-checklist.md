# Rust transport cut-over checklist (history)

> **History page.** This is the maintainer's own checklist for switching their live Home Assistant from the Go transport to the Rust one, kept as a record. **Its rollback procedure is OBSOLETE:** the Go rollback was retired (D-20261119-c1af, "we will not roll back to the Go wheel anymore") and its tooling (`packaging/rust/build_go_binaries.sh`, `release_go_rollback.py`, `release_dryrun.py --go-binaries`, `release_scheme.go_rollback_version`) and the Go source were deleted from `main` (D-20261123-cc1e). The commands below that name them no longer exist; they are in git history (commit `bdd8e61`). Read the rollback sections as history only. Release-host addresses are written as `<release-host>` and the account as `<owner>`. The source now pins the Rust wheel (`transport-rs-v0.7.1`), so the cut-over itself has happened; this page was not re-verified against the tags and the live host. The "live today" and "expect 0.6.10" statements below describe the situation before the cut-over. A stranger's installation never needs it.

**This was a supervised sitting.** Nothing below is done by an agent on its own: the cut-over switches the live Home Assistant
from the Go transport to the Rust one (a gate recorded in the maintainer's own task notes, not in this repository). Every step that touches the release host, the pin or the live HA needs the maintainer present.

## How the switch works (read once)

`custom_components/portico/door/wheel.py` installs the pinned wheel only when the installed wheel is OLDER than
`WHEEL_PIN_VERSION`. So:

* Cut-over = pin the Rust wheel (`0.7.x` is above the live Go `0.6.10`), so it installs.
* Rollback = pin the **Go build re-released at a version above every Rust version** (`0.9.x`). Repinning the old Go
  `0.6.10` does nothing while Rust `0.7.x` is installed. `wheel.py` is not changed for either.
* Versions and tags (`packaging/rust/release_scheme.py`):

| Lineage | Version | Tag | Wheel name |
| :--- | :--- | :--- | :--- |
| Go, live today | `0.6.10` | `transport-v0.6.10` | `portico_transport-0.6.10-py3-none-linux_x86_64.whl` |
| Rust | `0.7.x` | `transport-rs-v0.7.x` | `portico_transport-0.7.x-py3-none-linux_{x86_64,aarch64}.whl` |
| **Go rollback** | **`0.9.0`** (then `0.9.1`, ...) | **`transport-v0.9.0`** | `portico_transport-0.9.0-py3-none-linux_{x86_64,aarch64}.whl` |
| Rust after a rollback was pinned | `0.10.x` | `transport-rs-v0.10.0` | (a Rust `0.7.x` is below `0.9.0` and would be declined) |

The pin is per architecture: `WHEEL_PIN_URLS` in `wheel.py` maps `platform.machine()` (`x86_64`, `aarch64`) to a wheel URL;
`WHEEL_PIN_URL` stays the x86_64 one. The dry-run manifests print the lines to paste.

## Pre-conditions (all true BEFORE the pin change)

1. **The rollback wheel is built AND uploaded first.** A rollback that has to be built during an incident is not a rollback.
   `transport-v0.9.0` must exist as a Gitea release with both wheels, and the asset URLs must answer from the HA host.
   Build and dry-run: section "Build the rollback" below; upload: section "Upload".
2. The Rust release (`transport-rs-v0.7.0`) is built from a clean commit, its wheels uploaded (same procedure,
   `packaging/rust/release_dryrun.py`), and its sha256 recorded.
3. **There is no dev HA (the maintainer's decision): the production HA is the only one, so the rehearsal steps that needed a
   dev HA run INSIDE this sitting, with the rollback PR ready.** Done and recorded BEFORE the sitting, without HA:
   the datagram-flood measurement (PARITY 441) on the RELEASE build with two addresses on a real LAN
   (the build host had two: IPv4 and global IPv6 on `eth0`): a sender on each address floods garbage at the SFU port while
   a call runs; record that the device audio still reaches the station side and the CPU stays sane (today only the debug-build
   test `a_datagram_flood_on_several_addresses_does_not_starve_the_device_audio` covers it), the real-browser matrix
   (done 2026-11-17, INTEROP.md) and the aarch64 wheel run if an aarch64 host exists. Moved INTO the sitting (below): the
   upgrade, the rollback drill, the soak.
4. The pin PRs (cut-over and, prepared but unmerged, rollback) are open and reviewed; nobody else is merging to `main`.
5. A moment when a door-call interruption is acceptable. Note which wheel is installed now: on HA, `pip show portico-transport`
   in the HA container (expect `0.6.10`).

## The cut-over (in order)

1. Confirm pre-conditions 1 to 3 out loud. Stop here if the rollback wheel is not downloadable.
2. Merge the cut-over pin PR: it edits `WHEEL_PIN_VERSION = "0.7.0"` and `WHEEL_PIN_URLS` (both architectures, URLs ending
   `#sha256=<hex>` from the release manifest) in `custom_components/portico/door/wheel.py`, and updates the pin tests
   (`test_house_hub.py::test_the_floor_stayed_and_the_pin_...`). `hub.MIN_WHEEL_VERSION` stays unless the Python relies on
   something new.
3. Deploy the integration to HA (the usual path for `portico-ha` `main`) and reload the Portico integration or restart HA.
   The hub calls `async_ensure_wheel` on load: installed `0.6.10` < pin `0.7.0`, so it installs the Rust wheel (no restart is
   needed for the import, but the transport process is restarted by the reload).
4. Watch the HA log for `portico: installing the transport wheel 0.7.0` and then no `wheel_install_failed` repair issue.

## What to verify after

* `pip show portico-transport` says `0.7.0`, and `python -c "import portico_transport as p; print(p.BACKEND)"` says `rust`.
* The running transport process is the Rust binary (`portico-transport-linux-amd64`, static, from the new wheel), exactly one.
* Every door shows connected in the Portico panel; a test call in each direction (phone to door, door to phone) with audio;
  the gate/front-door talk path; a house call if used.
* No `wheel_install_failed` issue; the hub logs no restart loop for 10 minutes.
* Roaming (PARITY 441 / 440): a gate call on the Rust transport with the phone switched from Wi-Fi to
  mobile data mid-call; the call must continue (or re-establish within a few seconds) with audio both ways.
* RSS and CPU of the transport against the Go baseline recorded in the task page.
* **Rollback drill (was S4 on a dev HA; the maintainer's call whether to do it in the sitting):** with the Rust wheel verified, merge the
  prepared rollback pin PR, confirm `pip show portico-transport` says `0.9.0` and `p.BACKEND` says `go` and a test call works,
  then re-merge the cut-over (Rust) pin and confirm again. Two more short door-call interruptions; it proves the rollback on the
  real installer before it is needed. If skipped, the first real rollback is also the first rehearsal of it.
* **Soak (G3) runs on production after the sitting:** at least 7 days with RSS/CPU logged against the Go baseline; the rollback
  PR stays prepared and unmerged throughout.
* On an aarch64 host (if any): the aarch64 wheel installed (this binary has been linked but never run before the sitting).

## Roll back

Use it at the first sign of trouble (audio broken, restart loop, door offline). Pin the Go re-release, not the old pin:

1. Merge the prepared rollback pin PR: `WHEEL_PIN_VERSION = "0.9.0"`, `WHEEL_PIN_URLS` pointing at `transport-v0.9.0` (both
   architectures, with the `#sha256=` from the rollback manifest). Alternatively edit these two constants by hand.
2. Deploy and reload as in step 3. Installed `0.7.x` < pin `0.9.0`, so `wheel.py` installs the Go build.
3. Verify: `pip show portico-transport` says `0.9.0`, `p.BACKEND` says `go`, doors connected, test call.
4. Afterwards a Rust fix must be numbered `0.10.0` or higher (a `0.7.x` is below `0.9.0` and is declined). Cut it with
   `release_dryrun.py --version 0.10.0`.
5. If the rollback wheel is itself bad, a further rollback is `0.9.1` (the script picks the next one by itself).

## Build the rollback (offline of Gitea; nothing published)

```sh
cd repos/portico-ha            # a clean checkout of main
packaging/rust/build_go_binaries.sh transport-v0.6.10 /tmp/portico-go-out          # Docker, default bridge
export PORTICO_RELEASE_BASE_URL=http://<release-host>:3000/<owner>/portico-ha
packaging/rust/release_go_rollback.py --go-binaries /tmp/portico-go-out --go-ref transport-v0.6.10 --out /tmp/portico-go-rb
```

The dry run verifies the binaries' sums and that their `ref_commit` is what `transport-v0.6.10` is now, builds the two wheels
(byte-reproducible), and writes `manifest.json` and `SHA256SUMS`: the tag it would create (`transport-v0.9.0`), the upload URLs,
and the `pin_change_not_applied` block with the per-architecture URLs to paste into the rollback pin PR. Add
`--rollback-version 0.9.0` to pin the version explicitly (refused unless it is in the 0.9.x line or above and above every
existing transport tag). `release_dryrun.py --go-binaries ... --go-ref ...` builds the same wheels together with a Rust release.

## Upload (needs the maintainer's word; creates a tag)

Gitea API with a token (`write:repository`; the account administers `portico-ha`). Run from the dry-run output directory;
the release is created at the commit the Go binaries were built from.

```sh
BASE=http://<release-host>:3000; REPO=<owner>/portico-ha; TAG=transport-v0.9.0
COMMIT=$(git -C repos/portico-ha rev-parse 'transport-v0.6.10^{commit}')
RID=$(curl -sS -X POST "$BASE/api/v1/repos/$REPO/releases" -H "Authorization: token $GITEA_ANTIGRAVITY_AGENT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"tag_name\":\"$TAG\",\"target_commitish\":\"$COMMIT\",\"name\":\"Go transport rollback $TAG\",\"prerelease\":true,\"body\":\"Go 0.6.10 re-released above every Rust version (rollback for the Rust cut-over).\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
for f in /tmp/portico-go-rb/wheels-go-rollback/*.whl; do
  curl -sS -X POST "$BASE/api/v1/repos/$REPO/releases/$RID/assets?name=$(basename "$f")" \
    -H "Authorization: token $GITEA_ANTIGRAVITY_AGENT_TOKEN" -F "attachment=@$f" >/dev/null
done
# download back and compare with SHA256SUMS
for f in /tmp/portico-go-rb/wheels-go-rollback/*.whl; do
  curl -sS "$BASE/$REPO/releases/download/$TAG/$(basename "$f")" | sha256sum
done
cat /tmp/portico-go-rb/SHA256SUMS
```

The Rust release is the same with `TAG=transport-rs-v0.7.0` and the wheels from `release_dryrun.py`'s `wheels/` directory.
The tag is created by the release; a tag, once uploaded, is never reused or moved.
