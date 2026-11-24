# Go wheel release log (0.6.0 to 0.6.10)

> **History page.** The release notes of the Go transport wheels, written while they were released from the maintainer's own Gitea (the release host appears as `<release-host>` and the account as `<owner>`; asset ids, commit shas and sizes are as recorded then). The pinned wheel is now the Rust one (0.7.1); see [versions and tags](../reference/versions-and-tags.md). Kept for the facts about which wheel added which capability. **Pure history:** the Go source and build scripts these notes name (`transport/`, `packaging/real/build_real_wheels.sh`) were deleted from `main` after the cut-over (D-20261119-c1af, D-20261123-cc1e); the 0.6.10 state is the git tag `transport-v0.6.10` (commit `430b23f`), and the last `main` commit that still has the Go transport is `bdd8e61`. Rollback instructions in these notes are obsolete: there is no rollback to the Go wheel. Newest first. These sections were not re-verified.

## The wheel 0.6.10 release (wideband G.722 on the phone leg in the binary; RELEASED, pin PR `wheel-0610`)

**0.6.10 RELEASED (2026-11-12).** The Gitea release `transport-v0.6.10` (id 99, not a draft, not a prerelease, `target_commitish`
`430b23fa9f4268748c8e83c0f06496396a3017fd`, main after the merge of #282) carries three assets, built with `packaging/real/build_real_wheels.sh`
from a worktree of that commit with the version set to `0.6.10` (Docker on the default bridge, `golang:1.23-alpine`, `python:3.13-slim`):

| asset (id) | bytes | sha256 |
| :--- | :--- | :--- |
| `portico_transport-0.6.10-py3-none-linux_x86_64.whl` (132) | 8,243,504 | `d67546c95ffe8e42295a60178da4db821f4101e069b0291e32fd78fb466358d7` |
| `portico_transport-0.6.10-py3-none-manylinux2014_aarch64.whl` (133) | 7,727,596 | `0269301f0dcc9813f51232e7752586471b3c9373a594d9a92ba1b88cb9a51660` |
| `portico_transport-0.6.10-py3-none-linux_armv7l.whl` (131) | 7,513,341 | `6d05fe1b1d840816f0b9b667eb2557abfe7dfcdf7428bd13db83ad69680a9136` |

The embedded amd64 binary sha256 starts `44ceabb99d3af625` (15,127,080 bytes). The x86_64 wheel was downloaded again
at the pin URL (`http://<release-host>:3000/...`), with size and sha256 equal to the local file. **Change:** the Go side of PR #282: wideband
(G.722, 16 kHz) on the sfu phone leg with PCMU as the fallback; new Go dependency `github.com/gotranspile/g722` (resolved from the module
proxy during the build, present in all three binaries). PR #281 (`gate_sip_observer` drops stale audio queued while the phone connects) is
Python and ships with the integration, not the wheel. `MIN_WHEEL_VERSION` stays `0.5.0`. HA reinstalls the pin at the next restart.
Rollback: revert the pin PR and restart; the pin never downgrades an installed wheel, so a host already on 0.6.10 keeps it (uninstall the wheel
by hand to go back). The 0.6.9 release stays published.

## The wheel 0.6.9 release (door audio pacer in the binary; RELEASED; superseded by 0.6.10, pin PR `wheel-069`)

**0.6.9 RELEASED (2026-11-11).** The Gitea release `transport-v0.6.9` (id 97, not a draft, not a prerelease, `target_commitish`
`5dc1fdc9eb14ba8e063c807ac487b74404c96a8f`, main after the merge of #273) carries three assets, built with `packaging/real/build_real_wheels.sh`
from a worktree of that commit with the version set to `0.6.9` (Docker, `golang:1.23-alpine`, `python:3.13-slim`):

| asset (id) | bytes | sha256 |
| :--- | :--- | :--- |
| `portico_transport-0.6.9-py3-none-linux_x86_64.whl` (128) | 8,224,581 | `29642dabd34e88aa03da59ae5a56bf6df7711ff109037be576cd9427c08260e4` |
| `portico_transport-0.6.9-py3-none-manylinux2014_aarch64.whl` (129) | 7,702,044 | `349c44ae6526a8d9a87137403213908a2948582ec18c6228d4ae96cd605dd1e0` |
| `portico_transport-0.6.9-py3-none-linux_armv7l.whl` (127) | 7,494,477 | `a415112f0f8cf3fee4ce307b534e97b6013cff0cb23a75f6c29723cd028f3805` |

The embedded amd64 binary sha256 is `0b75c77b73a36270a1345d7122da58359e5c9aab54f932ef079b007b119e3f23`. The x86_64 wheel was downloaded again
at the pin URL (`http://<release-host>:3000/...`), with size and sha256 equal to the local file. **Change:** the Go side of PR #273: the adaptive
jitter buffer / pacer for front-door talk audio, both directions. `MIN_WHEEL_VERSION` stays `0.5.0`. HA reinstalls the pin at the next restart.
Rollback: revert the pin PR and restart; the pin never downgrades an installed wheel, so a host already on 0.6.9 keeps it (uninstall the wheel
by hand to go back). The 0.6.8 release stays published.

## The wheel 0.6.8 release (hangup-reason diagnostics in the binary; RELEASED; superseded by 0.6.9, pin PR `wheel-068`)

**0.6.8 RELEASED (2026-11-11).** The Gitea release `transport-v0.6.8` (id 89, not a draft, not a prerelease, `target_commitish`
`79ad6b3da4d4e00b992cd6a0eec996ae66617e10`, main after the merge of #262) carries three assets, built with `packaging/real/build_real_wheels.sh`
from a COPY of `packaging` and `transport` with the version set to `0.6.8` (Docker, `golang:1.23-alpine`, `python:3.13-slim`):

| asset (id) | bytes | sha256 |
| :--- | :--- | :--- |
| `portico_transport-0.6.8-py3-none-linux_x86_64.whl` (118) | 8,209,796 | `ec391adcbbe1fad8013b241aef2a4545a2c24a087fabd4a9f10b0d832af76a03` |
| `portico_transport-0.6.8-py3-none-manylinux2014_aarch64.whl` (119) | 7,693,951 | `364670fe8836265b3038e07503c217c3ea312ab9dff79f6bbc4cd5409d662651` |
| `portico_transport-0.6.8-py3-none-linux_armv7l.whl` (117) | 7,483,857 | `2fc7377d55f61a6f4f6714c590c5cb3ad830ed2f9d962478b7132eb0c1da3d75` |

The embedded amd64 binary sha256 is `cfcf38e60d57e53589e638f1565305f8b1ad9404c7904a0f9bac4c4c2262a11f`. The x86_64 wheel was downloaded again
at the pin URL (`http://<release-host>:3000/...`), with size and sha256 equal to the local file. **Change:** the Go side of PR #262: the transport
records who hung up and logs hangup and out-call outcomes. `MIN_WHEEL_VERSION` stays `0.5.0`. HA reinstalls the pin at the next restart.
Rollback: revert the pin PR and restart; the pin never downgrades an installed wheel, so a host already on 0.6.8 keeps it (uninstall the wheel
by hand to go back). The 0.6.7 release stays published.

## The wheel 0.6.7 release (front-door dial-out in the binary; RELEASED; superseded by 0.6.8, pin PR `wheel-0.6.7`)

**0.6.7 RELEASED (2026-11-11).** The Gitea release `transport-v0.6.7` (id 88, not a draft, not a prerelease, `target_commitish`
`c5227b3ad652b0aacb746dafe9a8f03b78e2f2e7`, main after the merge of #260) carries three assets, built with `packaging/real/build_real_wheels.sh`
from a COPY of `packaging` and `transport` with the version set to `0.6.7` (Docker, `golang:1.23-alpine`, `python:3.13-slim`):

| asset (id) | bytes | sha256 |
| :--- | :--- | :--- |
| `portico_transport-0.6.7-py3-none-linux_x86_64.whl` (115) | 8,207,376 | `ea4e0b32a72bc56c69e9125f47b9b76500e8763c7513e1ecea68aba69ffc971f` |
| `portico_transport-0.6.7-py3-none-manylinux2014_aarch64.whl` (116) | 7,691,144 | `20a0f1ca0f3c8e6ede0af35bdd2d133b7ff433deb4b789bf95a5cd993fd15708` |
| `portico_transport-0.6.7-py3-none-linux_armv7l.whl` (114) | 7,482,021 | `381d86415641f218336eebdb3196cffc4980c0c670c3a1ef81adfe996bd40f5b` |

The embedded amd64 binary sha256 is `afcf5bd4a08838dcc257f19c63188f2a0822968a516135ddad5a53c4be1b5861`. The x86_64 wheel was downloaded again
at the pin URL (`http://<release-host>:3000/...`), with size and sha256 equal to the local file. **Change:** the Go side of PR #260: the
`unifi_protect_talk` driver can dial out to the front door (CanDial, DialCall, ClaimedAudio), so the Doorbell Talk element is available at idle.
`MIN_WHEEL_VERSION` stays `0.5.0`. HA reinstalls the pin at the next restart. Rollback: revert the pin PR and restart; the pin never downgrades an
installed wheel, so a host already on 0.6.7 keeps it (uninstall the wheel by hand to go back). The 0.6.6 release stays published.

## The wheel 0.6.6 release (a repeated doorbell ring supersedes a still-ringing held call; RELEASED; superseded by 0.6.7, pin PR `wheel-0.6.6`)

**0.6.6 RELEASED (2026-11-10).** The Gitea release `transport-v0.6.6` (id 87, not a draft, not a prerelease, `target_commitish`
`c3ecd190148245883bc28cd1c55bdee3cbceb919`, main after the merge of #257) carries three assets, built with `packaging/real/build_real_wheels.sh`
from a COPY of `packaging` and `transport` with the version set to `0.6.6` (Docker, `golang:1.23-alpine`, `python:3.13-slim`):

| asset (id) | bytes | sha256 |
| :--- | :--- | :--- |
| `portico_transport-0.6.6-py3-none-linux_x86_64.whl` (112) | 8,202,941 | `646296946620a06986ee3927979e373950f6843b18506986cc57d28e3139ab16` |
| `portico_transport-0.6.6-py3-none-manylinux2014_aarch64.whl` (113) | 7,684,290 | `59e97a6383080ab40fbe78003484cc2d1a3f0d2ae90b71a50bbabad8d92e5f98` |
| `portico_transport-0.6.6-py3-none-linux_armv7l.whl` (111) | 7,478,785 | `85abfa3b01eb8c670a9c5aab7039daf5e6e3058ac9223d6b0aadd6ebc82c849c` |

The embedded amd64 binary sha256 is `0d0f25930baf1e5c6ddfe1094baf6cd5dd06cf30bddeff72f217d32b43b35833`. All three wheels were read back through the API (sizes);
the x86_64 wheel was also downloaded anonymously at the manifest's URL form, with size and sha256 equal to the local file. **Change:** the Go side of
PR #257: a new doorbell ring supersedes a still-ringing (unanswered) held call instead of being refused busy. `MIN_WHEEL_VERSION` stays `0.5.0`.
HA reinstalls the pin at the next restart. Rollback: revert the pin PR and restart; the pin never downgrades an installed wheel, so a host already on
0.6.6 keeps it (uninstall the wheel by hand to go back). The 0.6.5 release stays published.

## The wheel 0.6.5 release (front-door talk: `unifi_protect_talk` in the binary, inert; RELEASED; superseded by 0.6.6, pin PR `wheel-0.6.5`)

**0.6.5 RELEASED (2026-11-10).** The Gitea release `transport-v0.6.5` (id 86, not a draft, not a prerelease, `target_commitish`
`8ba46c87a9a42beb3d56de390bc740b9498701d4`, main after the merge of #254) carries three assets, built with `packaging/real/build_real_wheels.sh`
from a COPY of `packaging` and `transport` with the version set to `0.6.5` (Docker, `golang:1.23-alpine`, `python:3.13-slim`):

| asset (id) | bytes | sha256 |
| :--- | :--- | :--- |
| `portico_transport-0.6.5-py3-none-linux_x86_64.whl` (109) | 8,201,791 | `232359462c1daecc17b8588abc5bc5c9a50bdaf7cbd5f34aeaed5d9994446d54` |
| `portico_transport-0.6.5-py3-none-manylinux2014_aarch64.whl` (110) | 7,683,864 | `e5d9db407004bf8c1e722fb91cf32dde899c090bf57f347c696bb00488962aa5` |
| `portico_transport-0.6.5-py3-none-linux_armv7l.whl` (108) | 7,478,497 | `ad168dcf776143792e1056aace977d710370dc5156e72a7d1bf14aeaf9634496` |

The embedded amd64 binary sha256 is `57856c25fbcd27272a37fd68512b950a7bbdeb9bcc063d28e568f99af2155e4b`. All three wheels were read back through the API (sizes);
the x86_64 wheel was also downloaded anonymously at the manifest's URL form, with size and sha256 equal to the local file. **Change:** the Go side of the
front-door talk plan (`unifi_protect_talk`, `external_ring`, `talk_health`, `call_reason`; PRs #251-#254). `MIN_WHEEL_VERSION` stays `0.5.0`: an `ha_event`
entry with `talkback` off never touches the hub, and one with it on falls back to ring-only if the wheel lacks the driver
(`door.py` `_attach_talk`; `ha_event.TALKBACK_MIN_WHEEL` is documentation of the first wheel that has it). Nothing is answerable until an entry sets
`talkback: true` with its own Protect key (T7, the maintainer's word); the option defaults off and no existing entry sets it. HA reinstalls the pin at the next
restart. Rollback: revert the pin PR and restart; note the pin never downgrades an installed wheel, so a host already on 0.6.5 keeps it (uninstall the
wheel by hand to go back). The 0.6.4 release stays published.

## The wheel 0.6.4 release (gate_sip_server in the binary, inert; RELEASED, pin PR `wheel-0.6.4`)

**0.6.4 RELEASED (2026-11-09).** The Gitea release `transport-v0.6.4` (id 79, not a draft, not a prerelease, `target_commitish`
`c1a2b01ae84c0e1539811961c9540cc70621bed1`, main after the merge of #221) carries three assets, built with `packaging/real/build_real_wheels.sh`
(Docker, `golang:1.23-alpine`, `python:3.13-slim`; go.mod says go 1.23):

| asset (id) | bytes | sha256 |
| :--- | :--- | :--- |
| `portico_transport-0.6.4-py3-none-linux_x86_64.whl` (100) | 8,125,391 | `d190ff17b8d8bfb0a00a4883ff54cdcbee23b72cb3fb5610b9ebf3922aaf1aca` |
| `portico_transport-0.6.4-py3-none-manylinux2014_aarch64.whl` (101) | 7,614,266 | `6752d91a549fcc983e4ed06571cb8882e6eefecb44e517291c16cd3eef3c1a91` |
| `portico_transport-0.6.4-py3-none-linux_armv7l.whl` (99) | 7,412,974 | `952cedece4eb3bd478b4afd6a8778aa3b483a86eb20c61bd4e97cff819912ff5` |

The embedded amd64 binary sha256 is `1a577b3c9fc08048a3a516748f450b8767202f755ab2e2a3852a995b163f13c5`. All three wheels were read back through
the API (sizes) and anonymously at the manifest's URL form; size, sha256 and bytes match the local files. **Change:** the binary registers the
`gate_sip_server` driver (Portico's own SIP server mode: answer audio via ConnectAnswerer, outbound call via CallDialer, bad digests refused, 487
after CANCEL; #214, #216, #218, #213). Inert until a server entry is created: behaviour with no server entry is identical to 0.6.3.
`MIN_WHEEL_VERSION` stays `0.5.0`. HA reinstalls the pin at the next restart. Rollback: revert the pin PR and restart; note the pin never
downgrades an installed wheel, so a host already on 0.6.4 keeps it (uninstall the wheel by hand to go back). The 0.6.3 release stays published.

## The wheel 0.6.3 release (gate-sip observer republishes CanDial; RELEASED, pin PR `wheel-0.6.3`)

**0.6.3 RELEASED (2026-11-09).** The Gitea release `transport-v0.6.3` (id 76, not a draft, not a prerelease, `target_commitish`
`1dae2a45ae8e7b09465b50099adf6b27b7abe2de`, main after the merge of #211) carries three assets, one per platform (this release attaches all
three; 0.6.0 to 0.6.2 attached only the x86_64 one):

| asset (id) | bytes | sha256 |
| :--- | :--- | :--- |
| `portico_transport-0.6.3-py3-none-linux_x86_64.whl` (95) | 7,961,621 | `cc9ba9b869b668fecb3b1dd36239d9507d22cc12c0a88372e2a85403fd094320` |
| `portico_transport-0.6.3-py3-none-manylinux2014_aarch64.whl` (96) | 7,456,337 | `e11f490c8c220626ef64e82289d7ad6c0ec7908018b9b87ec8e90c2d79c39b75` |
| `portico_transport-0.6.3-py3-none-linux_armv7l.whl` (94) | 7,276,486 | `a84f3693d613703a644619852e5cf1f14907c7ef9631e1e0082d47515c18d44d` |

The embedded amd64 binary sha256 is `f56cdd017b114c0865654f0a25c5be0149af37ed70cf2229dd81dc8f52668156`. The x86_64 wheel was read back through the API (sizes) and
anonymously at the manifest's URL form with the same size and sha256. **Change:** the gate-sip observer driver republishes `CanDial` through a new,
additive `capabilities_changed` IPC notification when gate-sip's dial switch flips at runtime (#211); Go only plus that notification.
`MIN_WHEEL_VERSION` stays `0.5.0`: the Python side of #211 degrades with an old wheel. The pin PR sets `version = "0.6.3"` and moves the pin
and the tests that name it. The offline test venv needs no rebuild (the tests assert the pin string, not the installed wheel). The running Home
Assistant stays on 0.6.2 until the pin PR merges and HA restarts (HA reinstalls the pin at the next restart). Rollback: revert the pin PR and
restart; note the pin never downgrades an installed wheel, so a host already on 0.6.3 keeps it (uninstall the wheel by hand to go back). The
0.6.2 release stays published.

## The wheel 0.6.2 release (PCMU-only answer, gate-microphone gain, sfu logging; RELEASED, pin PR `release/wheel-0.6.2-pin`)

**0.6.2 RELEASED (2026-10-22).** The Gitea release `transport-v0.6.2` (id 45, not a draft, not a prerelease, `target_commitish`
`48280adbe5c1961081e712ed735934d4902795cf`, the merge of #160) carries the one asset `portico_transport-0.6.2-py3-none-linux_x86_64.whl`
(asset id 63), **8,344,506 bytes, sha256 `cfa3a8c32b92b3072212998193ff8044c6226753b5af0529b3dbf8c2205c4dcd`** (embedded binary sha256
`76a1593a69c49667ee389690b1abe3c4c657b136cacc065ad5eb699fa4999bfb`), read back through the API and anonymously at the manifest's URL form
with the same size and sha256. **Cause of the fix:** the live Portico gate-answer tests found two real audio bugs. The hub answered the
phone's offer with Opus first, so the phone sent Opus and the hub decoded it as mu-law (crackle); 0.6.2 answers PCMU only. The gate
microphone was played to the phone at unity gain and was very faint; 0.6.2 amplifies it. It also adds sfu logging of the WebRTC leg. The change
is in the Go binary only; no IPC verb changed. `MIN_WHEEL_VERSION` stays `0.5.0`: nothing a door needs requires 0.6.2, and a raised floor
would only make `wheel_too_old` fire on old wheels. The pin PR sets `version = "0.6.2"`, moves the manifest pin and the tests that name it, and
the offline venv is rebuilt as `portico-py314-venv-2610-5`. The running Home Assistant stays on 0.6.1 until the pin PR merges and HA
restarts; after that restart the audio fixes are live and nothing else changes. Rollback: revert the pin PR and restart (HA reinstalls the
0.6.1 pin; the 0.6.1 release stays published).

## The wheel 0.6.1 release (phone uplink jitter-buffer fix; RELEASED, pin PR `release/wheel-0.6.1-pin`)

**0.6.1 RELEASED (2026-10-22).** The Gitea release `transport-v0.6.1` (id 44, not a draft, not a prerelease, `target_commitish`
`ec2aacd0c6d9cfd8feb5c312a418c9e2e0ba7fda`, the merge of #158) carries the one asset `portico_transport-0.6.1-py3-none-linux_x86_64.whl`
(asset id 62), **8,335,977 bytes, sha256 `3f8abfe5399414634a8a94ea6205b8a00e35cb72759d1c7d0242dea6bf26dbc3`** (embedded binary sha256
`5703b201c0304369506476298d73f8083fc717003385e032a8106e919daf1ce2`), read back through the API and anonymously at the manifest's URL form
with the same size and sha256. **Cause of the fix:** the first live Portico answer chopped the phone-to-gate audio. The uplink jitter buffer
assumed 40 ms packets and phones send 20 ms ones (#158 sizes it by the phone's packet size). The change is in the Go binary only; no IPC verb
changed. `MIN_WHEEL_VERSION` stays `0.5.0`: nothing a door needs requires 0.6.1, and a raised floor would only make `wheel_too_old` fire on old wheels.
The pin PR sets `version = "0.6.1"`, moves the manifest pin and the tests that name it, and the offline venv is rebuilt as
`portico-py314-venv-2610-4`. The running Home Assistant stays on 0.6.0 until the pin PR merges and HA restarts; after that restart the
phone-to-gate audio fix is live and nothing else changes. Rollback: revert the pin PR and restart (HA reinstalls the 0.6.0 pin; the 0.6.0
release stays published).

## The wheel 0.6.0 release, as a checklist (house-call bridge; M7 3b-S, 3g; RELEASED, pin PR pending the rig)

The Python half of the bridge (3b-S) is feature-detected and merged before the wheel exists, with `MIN_WHEEL_VERSION` and the
manifest pin untouched. Releasing the wheel is a separate, later step, taken on the maintainer's word, in this order. Each step is
a decision of his or a host-CLI action; nothing here is done by merging the 3b-S or 3g PRs.

**RELEASED (2026-10-22).** Steps 1 to 3 are done: the Gitea release `transport-v0.6.0` (id 42, not a draft, not a prerelease, `target_commitish` `4232d1e69c9c1de4fc719724b6156484ad2cddf4`) carries the one asset `portico_transport-0.6.0-py3-none-linux_x86_64.whl` (asset id 60), **8,333,618 bytes, sha256 `d7ae1d004279c1df8652b95c423574f305521d1fd4dc8bcebc72ee30c6edbdcf`**, read back through the API and anonymously at the manifest's URL form with the same size and sha256. This is the authoritative artifact; the dry-run sha256 below belongs to a rebuild and differs. The running Home Assistant stays on 0.5.1 until step 4 merges and HA restarts; step 4 (the pin PR) must not merge before the maintainer has said the gate-sip rig (`e2e/gatesip/run_e2e.sh`, `RUNS=3`) passed.

**What the 3g dry run found (2026-10-22; a rehearsal before the release above).** The wheel was built from the 3g
branch exactly as 0.5.1 was, in a temp directory, and checked offline:

| | |
| :--- | :--- |
| file | `portico_transport-0.6.0-py3-none-linux_x86_64.whl` |
| size | 8,333,618 bytes (0.5.1: 8,288,595) |
| sha256 | `adfbbf3f227d0e02cf36c5a2c025598af45177ba06e3bff26a17482adebf2ec9` |
| embedded binary | `portico_transport/portico-transport-linux-amd64`, 15,393,960 bytes, mode 0755, sha256 `6b30a14f4d3d4314cc9e44d8e89a2bb2f73653cf83c6b1a519b6e9fbd6a0e2fd` |
| members | `portico_transport/__init__.py`, the one binary, and the `dist-info` (METADATA `Version: 0.6.0`, tag `py3-none-linux_x86_64`) |

This is a rehearsal, **not the release artifact**: the release is rebuilt from `main` after 3g merges (the zip carries
timestamps, so the sha256 of any rebuild differs; only the one uploaded is authoritative, so compare the uploaded bytes, not a
rebuild). What was proved on the built wheel, offline, with `--network none`: the binary started with `-socket S -house-calls`
answers `house_open` (two `frames` legs: `session` `house-<hex>`, `frame_bytes` 1280, `frame_ms` 40), forwards five frames from
one leg to the other, answers `house_close` `{ok: true, closed: true}` and a second `house_close` `{ok: true, closed: false}`,
and writes ONE log line when the call ends (`portico-transport: house call hso-v1 ended reason=hangup cause=- dur=0s from[frames
rx=5 tx=0 ...] to[frames rx=0 tx=5 ...]`); started without the flag it answers both verbs `not_supported` and writes nothing; and
the **0.5.1 binary given `-house-calls` exits with status 2** (`flag provided but not defined: -house-calls`), which is why
the hub (`hub.py`, `house_calls_wanted`) never sends the flag to a wheel older than 0.6.0. The Go suite (`go test ./...`, and `-race` on
`internal/house`) passes. **Not run in the dry run:** the gate-sip rig (`e2e/gatesip/run_e2e.sh`, `RUNS=3`), which is step 1 below.

**Two corrections to how `packaging/real/build_real_wheels.sh` reads.** (a) It builds all three platforms with `golang:1.23-alpine`
and fetches modules during the build (network); 0.5.1 was not built that way and neither was this dry run: Go 1.24, modules fetched
once into a private cache and verified against `go.sum`, then the build itself with `--network none`. (b) Only x86_64 is built and
published (the HA box is x86_64); the arm wheels are not part of the release. The packaging step still needs the network once,
to `pip install build wheel` (the stock images have neither), on Docker's default bridge , never on a
network of its own.

1. Confirm `transport/` on `main` is what 0.6.0 should contain: the bridge (`transport/internal/house`, 3b-Go #152), its per-leg
   counters (3g), `-house-calls` off by default, P3-A's `CanDial` (already in 0.5.1). `go test ./...` and the rig
   (`e2e/gatesip/run_e2e.sh`, `RUNS=3`) pass.
2. Build, from a clean clone of `main` (all in a temp directory `$O`; `$CL` is the clone; the clone stays clean):
   ```
   # A. once, network: fetch the modules go.mod/go.sum name into a private cache
   docker run --rm -e GOFLAGS=-mod=readonly -v $O/modcache:/go/pkg/mod -v $CL/transport:/src:ro -w /src golang:1.24 go mod download
   # B. offline: tests, then the static linux/amd64 binary
   C="--rm --network none -e GOFLAGS=-mod=readonly -e GOPROXY=off -e GOCACHE=/gocache -v $O/modcache:/go/pkg/mod -v $O/gocache:/gocache"
   docker run $C -v $CL/transport:/src -w /src golang:1.24 go test -count=1 ./...
   docker run $C -e CGO_ENABLED=0 -e GOOS=linux -e GOARCH=amd64 -v $CL/transport:/src:ro -v $O/bin:/out -w /src golang:1.24 \
     go build -o /out/portico-transport-linux-amd64 ./cmd/portico-transport
   # C. package from a COPY of packaging/real with the version set (the release PR commits the same version bump)
   mkdir -p $O/pkg && cp -r $CL/packaging/real/pyproject.toml $CL/packaging/real/portico_transport $O/pkg/
   sed -i 's/^version = "0.5.1"/version = "0.6.0"/' $O/pkg/pyproject.toml
   cp $O/bin/portico-transport-linux-amd64 $O/pkg/portico_transport/
   docker run --rm -v $O/pkg:/build -w /build python:3.13-slim bash -c '
     pip install --quiet build wheel && python -m build --wheel -o dist_tmp &&
     python -m wheel tags --platform-tag linux_x86_64 --remove dist_tmp/*.whl &&
     mkdir -p dist && mv dist_tmp/*.whl dist/ && chmod -R a+rwX /build'
   ```
   Then read the wheel back (`python -m zipfile -l`; name, size, sha256, the binary's ELF header and mode) and run the offline
   check described above against it before anything is uploaded.
3. Publish the Gitea release `transport-v0.6.0` (not a draft, not a prerelease, `target_commitish` the exact `main` sha built) with the
   one asset `portico_transport-0.6.0-py3-none-linux_x86_64.whl`; fetch it back through the API and anonymously with the pin's
   URL form (`WHEEL_PIN_URL`) and compare size and sha256 with the local file. **Do this before any pin moves**: with the pin bumped and the asset
   missing, the hub cannot install the wheel, raises the repair issue `wheel_install_failed` and the door waits (the door observer is
   down; phones, push and the panel keep running).
4. The release PR (separate, after the release exists; **prepared as the pin PR `release/wheel-0.6.0-pin`, floor kept at `0.5.0`, the offline venv rebuilt as `portico-py314-venv-2610-3`**): set `version = "0.6.0"` in `packaging/real/pyproject.toml`; move the pin in
   `custom_components/portico/door/wheel.py` (`WHEEL_PIN_VERSION` and `WHEEL_PIN_URL`; the manifest carries no wheel); set `MIN_WHEEL_VERSION = "0.6.0"` in `door/hub.py` **only if** the Python then
   relies on a 0.6.0 verb for something a door needs (3b-S and 3g do not: the house paths are feature-detected, so the floor may
   stay `0.5.0`; decide there); update `test_wheel.py`/`test_manifest.py` if they name the version, `scripts/build_test_venv.sh`'s default `WHEEL_URL` and the comment
   in `scripts/test_offline.sh`, and the docs that name the wheel (`README.md`, `door/README.md`, this file). Rebuild the
   offline test venv with the 0.6.0 wheel (`scripts/build_test_venv.sh <volume> 2026.10.2`; the 3g form tests patch the wheel
   version, so they pass on either wheel).
5. After the merge and an HA restart onto the 0.6.0 wheel nothing changes yet: the flag is still not passed. From then on the
   observer's Reconfigure form shows **Phone-to-phone house calls** (3g; it is hidden while the wheel is older than 0.6.0). The wheel
   reaches the box by the hub's lazy install at the first door start after the restart (it logs `portico: installing the transport
   wheel 0.6.0`; no manifest requirement is involved). Turning the
   bridge on is a further step: switch it on in that form (it is stored as the option `house_calls` on the door entry), save, and
   **restart Home Assistant**: the option is read only when the Portico hub process starts, and reloading one door does not restart
   it while other doors keep it running. Also switch on the Portico settings entry's house calls (the call registry). Both are off by default
   and each is the maintainer's word. The first call between two phones needs the second enrolled phone.
6. Reading the parity numbers (3e): every house call writes one line to the Home Assistant log when it ends (logger
   `custom_components.portico.door.hub`, level INFO, prefix `portico transport: portico-transport: house call hso-...`): reason, the
   leg that ended it, duration, and per leg `rx` (frames received from the phone), `tx` (frames given to the phone), `dropped` (the
   phone's downlink was full), `bad` (wrong-size frames), `concealed` (uplink frames missing, estimated from gaps of 60 ms or more)
   and `maxgap` (the largest gap between two uplink frames). The app's own counters are the app's (3b-A).
7. Rollback: revert the release PR and restart. **That alone does not downgrade the wheel any more** (before the unification the
   manifest pin made HA reinstall the older pin at every load; the lazy install only ever upgrades): the 0.6.0 wheel stays installed
   and, being newer than the reverted pin, is left alone. To go back to an older wheel install it by hand (`pip`/`uv pip install` of
   the older release asset in the Home Assistant container; **UNVERIFIED on HA OS**) and restart. The Python needs no revert, it detects
   the wheel version and goes back to `not_supported` on an old one, and a saved `house_calls` option stays in the entry, harmless and
   still shown (so it can be switched off). A 0.5.1 binary is never given `-house-calls`.
