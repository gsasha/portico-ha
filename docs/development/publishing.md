# Publishing to HACS

What it takes to publish this repository as a HACS custom repository, what is already in place, and what needs a decision before the first public release. Nothing is published yet.

Up: [documentation index](../README.md). The release log of the older Go wheels is in [history](../history/go-wheel-releases.md).

## Status in one paragraph

The repository is HACS-shaped already: one integration at `custom_components/portico/`, a `manifest.json` with the keys HACS asks for, a root `README.md`, a `LICENSE` (MIT) and, as of this page, a `hacs.json`. Two things block a real publication. **The transport wheel is pinned to URLs on an internal host that nobody outside the maintainer's network can reach**, so an installation from HACS would load the integration and then fail to start any door (see [the wheel pin](#the-wheel-pin-is-the-blocker)). And a few **decisions** are open (list at the end), among them where the public repository lives: HACS only lists public GitHub repositories.

## What HACS requires

Checked against the HACS publishing documentation on 2026-11-18 ([integration](https://www.hacs.xyz/docs/publish/integration/), [general requirements](https://www.hacs.xyz/docs/publish/start/)). HACS changes; re-read those pages before the first release.

| Requirement (HACS wording, shortened) | Here |
| :--- | :--- |
| One integration per repository, under `custom_components/<name>/` | Yes: `custom_components/portico/`. |
| `manifest.json` defines `domain`, `documentation`, `issue_tracker`, `codeowners`, `name`, `version` | All six are present (checked in `custom_components/portico/manifest.json`; `version` is `0.3.0`). The `documentation` and `issue_tracker` URLs point at `github.com/gsasha/portico-ha`; change them if the public repository has another name. |
| `hacs.json` in the repository root; `name` is its one required key | Added: `{"name": "Portico"}`. No other key is set on purpose (see the decisions). |
| Brand assets: a `brand` directory with at least `icon.png` | `custom_components/portico/brand/` holds `icon.png` and `icon@2x.png`. The HACS page does not say where the directory goes; it sits inside the integration folder, which is where Home Assistant reads brand images for a custom integration. **Not verified against HACS's own validation**; run the HACS validation action once the repository is public. Whether the images are fit to publish (rights, design) is for the maintainer. |
| A public GitHub repository with a short description and topics | **Not met.** The development repository is a private Gitea. |
| A `README.md` explaining use | Yes, [`README.md`](../../README.md). |
| Releases rather than bare tags ("just publishing tags is not enough") | **Not met.** Only tags exist, and the integration's own tag `portico-v0.3.0` has not been cut (the only `portico-v*` tag is `portico-v0.1.0`). See [versions and tags](../reference/versions-and-tags.md). |

HACS copies the whole `custom_components/portico/` folder into a user's configuration. That includes `tests/` and `www/tests/` (with a `package-lock.json`). HACS can serve a zip asset from a release instead (`zip_release` plus `filename` in `hacs.json`), which would leave the tests out; whether to do that, or to move the tests out of the component folder, is a decision (below). Not adding `zip_release` now keeps the first publication simple.

## The wheel pin is the blocker

`custom_components/portico/door/wheel.py` pins the transport wheel by URL, one per architecture (`WHEEL_PIN_URLS`, `x86_64` and `aarch64`, version 0.7.4, with the `sha256` in the URL fragment). The URLs name the GitHub release `transport-rs-v0.7.4` of this repository, so the release must exist with those exact wheel files before a Home Assistant elsewhere can install the wheel. Until it does, a Home Assistant without the wheel gets a connection error, raises the repair issue `wheel_install_failed`, and every door waits. Phones, push and the panel still load. An installation that hosts the same release files elsewhere (a private mirror) sets the `wheel_base_url` option of the Portico settings entry; the path and the hash stay as pinned.

What has to change before publishing:

1. The wheels must be hosted where anyone can fetch them without a login.
2. `WHEEL_PIN_URLS` in `door/wheel.py` must name those URLs. The `sha256` fragments stay the same only if the very same wheel files are uploaded; a rebuild has a different hash (the zip carries timestamps), so either upload the existing files or update the hashes.
3. Check the places that read the pin: `custom_components/portico/tests/test_wheel.py` imports the constants (it should follow without edits, unverified), and `scripts/build_test_venv.sh` reads `WHEEL_PIN_URLS` to download the wheel for the offline test venv.
4. `WHEEL_PIN_VERSION` and the release tags move together as before: a wheel release is a change to the two constants, not to `manifest.json`.

Options for the hosting:

* **GitHub release assets on the public repository** (for example `https://github.com/<owner>/portico-ha/releases/download/transport-rs-v0.7.3/portico_transport-0.7.3-py3-none-linux_x86_64.whl#sha256=...`). Closest to what exists: same file names, same pin mechanism, no new package index. It needs the public repository and a release for each wheel version. Home Assistant's installer does fetch such URLs (it is the same requirement installer the current pin uses), though not checked here against GitHub's redirect to its asset store.
* **PyPI** (a normal `portico_transport==0.7.3` requirement). To my knowledge PyPI refuses wheels with a bare `linux_x86_64` or `linux_aarch64` platform tag, which is what these wheels carry (the binary is static, so a `musllinux` or `manylinux` tag may be possible). **Unverified**: the wheel would have to be re-tagged and re-tested, and the package name would have to be free. It would also put a compiled binary on a public index under the maintainer's name, which is a policy decision of its own. Moving to a manifest requirement would also give up the reason the pin lives in code (a manifest URL requirement is reinstalled on every load); a plain version requirement does not have that problem.
* **Build on the user's machine**: not offered; the Rust toolchain build is Docker-based and heavy.

A self-hoster can bypass the problem today by installing the wheel by hand; see [prerequisites](../getting-started/prerequisites.md#the-transport-wheel).

## Publishing the repository

How the repository gets from the private Gitea to a public GitHub repository is open. Two ways, with the trade-off:

* **Keep the history.** Mirror or push the existing history. It keeps authorship and `git blame`, but the history has not been audited and the tree itself still holds some estate details in tests, fixtures and the Go test data (addresses and names from one house). A history rewrite would be needed to scrub them, and a rewrite changes every tag.
* **Start a new history** from a clean export of the current tree (`git archive`). It publishes nothing that was ever committed and removed, and loses blame.

Either way, scrub the tree first. HACS expects plain version tags and GitHub releases; the existing tag families are in [versions and tags](../reference/versions-and-tags.md).

What does not split cleanly if only the integration folder were published:

* The tests read the shared `contract/` directory (`push_v2.json`, `ws_v3.json`, `ws_media_v2.json`, `app_commands_v3.txt`, `icons_v1.json`). The JSON fixtures are also vendored by the phone app with pinned hashes, so moving them is a change on that side too.
* Some tests (`tests/test_strings.py`) read `README.md` and every page under `docs/` for operating-guide text.
* `transport-rs/`, `packaging/` and `e2e/` are not under `custom_components/`. They are the source of the wheel; whether they live in the same repository or in their own is a decision.

There is no CI. A public CI workflow would replace the local Docker scripts (`scripts/test_offline.sh`); the suite needs Home Assistant and its test plugin.

## Decisions needed

1. **Where the public repository lives and what it is called** (GitHub; the name in `manifest.json` URLs).
2. **Where the wheels are hosted** (GitHub release assets or PyPI; see above), and then moving the pin.
3. **History or fresh start**, and a scrub of the tree either way.
4. **Whether the transport sources and packaging live in the same repository.**
5. **Tests in the component folder**: leave them in (they are installed into users' configurations), ship a `zip_release`, or move them out.
6. **A minimum Home Assistant version for `hacs.json`** (`homeassistant` key). Not set: the tests run on one version only, 2026.10.2, so there is no tested floor.
7. **Brand images**: confirm they may be published, and that HACS accepts the current location.
8. **A security contact** for [CONTRIBUTING](../../CONTRIBUTING.md) (a placeholder is there).
