#!/usr/bin/env python3
"""Dry-run of a transport release (DEV-764 Phase 4): build everything, write a manifest, STOP.

    release_dryrun.py --version 0.7.0 --out DIR [--binaries DIR] --base-url URL [--allow-dirty]

What it does: (1) cross-builds the Rust binaries with build_rust_binaries.sh (or takes a prebuilt directory),
(2) builds the Rust wheels, (3) writes DIR/manifest.json and DIR/SHA256SUMS with the size and sha256 of every
artefact, the tag it WOULD create, the asset URLs it WOULD upload to, and the `door/wheel.py` lines a pin change WOULD
carry. It never uploads, never creates or pushes a tag, never edits a pin. The only git it runs is read-only
(`_git` refuses anything else); there is no network code in this file. The real release is a separate, human-run step
that the owner approves (the GATE in docs/tasks/portico-rust-transport.md).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_wheels  # noqa: E402
import release_scheme as scheme  # noqa: E402

REPO = HERE.parent.parent
BASE_URL_ENV = "PORTICO_RELEASE_BASE_URL"     # no default in the source: the release host is not published (R1-49)
_READ_ONLY_GIT = {"rev-parse", "status", "log", "describe", "tag", "show"}


def _git(*args: str, cwd: Path | None = None) -> str:
    """Read-only git. `tag` is allowed only as a listing (-l / --list); everything not in the allow-list is refused."""
    if not args or args[0] not in _READ_ONLY_GIT:
        raise RuntimeError(f"release_dryrun runs read-only git only, refused: git {' '.join(args)}")
    if args[0] == "tag" and not (len(args) >= 2 and args[1] in ("-l", "--list")):
        raise RuntimeError(f"release_dryrun may list tags, never create them: git {' '.join(args)}")
    return subprocess.run(["git", "-C", str(cwd or REPO), *args], check=True, capture_output=True, text=True).stdout.strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def existing_versions() -> list[str]:
    """Versions of every transport tag already in the repo, both families (a new release must beat all of them)."""
    out = []
    for line in _git("tag", "-l", "transport-v*", "transport-rs-v*").splitlines():
        try:
            out.append(scheme.parse_tag(line.strip())[1])
        except scheme.SchemeError:
            pass
    return out


def artefact(path: Path, kind: str, **extra) -> dict:
    return {"file": path.name, "kind": kind, "size": path.stat().st_size, "sha256": sha256_file(path), **extra}


def pin_snippet(base_url: str, tag: str, version: str, wheels: dict[str, dict]) -> dict:
    """The lines of door/wheel.py a pin change would carry, PER ARCHITECTURE (`wheels` maps arch -> {"file", "sha256"}).
    Printed into the manifest, applied nowhere. Each URL carries `#sha256=<hex>` (pip verifies a hash fragment on a direct
    URL). `WHEEL_PIN_URL` is the x86_64 one, kept as the name the constant has today; `WHEEL_PIN_URLS` is the per-arch map
    (architectures as `platform.machine()` spells them)."""
    urls = {arch: f"{scheme.asset_url(base_url, tag, w['file'])}#sha256={w['sha256']}" for arch, w in sorted(wheels.items())}
    return {
        "WHEEL_PIN_VERSION": version,
        "WHEEL_PIN_URLS": urls,
        "WHEEL_PIN_SHA256S": {arch: w["sha256"] for arch, w in sorted(wheels.items())},
        "WHEEL_PIN_URL": urls.get("x86_64"),
        "WHEEL_PIN_SHA256": wheels["x86_64"]["sha256"] if "x86_64" in wheels else None,
        "applied": False,
        "note": "door/wheel.py is NOT edited by this script; the pin change is its own PR, merged on the owner's word",
    }


def verify_prebuilt(bindir: Path, commit: str, allow_dirty: bool = False) -> dict:
    """R1-35: prebuilt binaries are accepted only with the SHA256SUMS and build-info.txt that build_rust_binaries.sh wrote,
    the sums must match the files, and the build must be of this HEAD from a clean transport-rs. Returns what to record."""
    sums_path, info_path = bindir / "SHA256SUMS", bindir / "build-info.txt"
    for need in (sums_path, info_path):
        if not need.is_file():
            raise SystemExit(f"--binaries {bindir}: {need.name} is missing (it is written by build_rust_binaries.sh); "
                             "binaries without their sums and build info are not accepted")
    listed = {}
    for line in sums_path.read_text().splitlines():
        if line.strip():
            digest, _, name = line.partition("  ")
            listed[name.strip()] = digest.strip()
    for name, digest in listed.items():
        f = bindir / name
        if not f.is_file():
            raise SystemExit(f"SHA256SUMS names {name}, which is not in {bindir}")
        if sha256_file(f) != digest:
            raise SystemExit(f"SHA256SUMS mismatch for {name}: the file is not the one that was summed")
    info = dict(l.split("=", 1) for l in info_path.read_text().splitlines() if "=" in l)
    if info.get("commit") != commit:
        raise SystemExit(f"build-info.txt says commit={info.get('commit')} but HEAD is {commit}: rebuild from this commit")
    if info.get("transport-rs") != "clean" and not allow_dirty:
        raise SystemExit(f"build-info.txt says transport-rs={info.get('transport-rs')}: the binaries were built from a dirty tree")
    return {"sha256sums": sums_path.read_text(), "build_info": info_path.read_text(), "listed": listed}


def run(args) -> dict:
    out = args.out.resolve()
    commit = _git("rev-parse", "HEAD")
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH") or _git("log", "-1", "--format=%ct"))
    dirty = bool(_git("status", "--porcelain", "--", "transport-rs", "packaging/rust"))
    if dirty and not args.allow_dirty:
        raise SystemExit("transport-rs/ or packaging/rust/ has uncommitted changes: a release is built from a commit "
                         "(commit first, or pass --allow-dirty for a throwaway run)")
    if not args.base_url:
        raise SystemExit(f"no base URL: pass --base-url or set {BASE_URL_ENV}")
    out.mkdir(parents=True, exist_ok=True)
    taken = existing_versions()
    tag = scheme.tag_for("rust", args.version)
    if args.version in taken:
        raise SystemExit(f"{tag} already exists in this repository: a release version is never reused")

    bindir = args.binaries.resolve() if args.binaries is not None else None
    if bindir is None:
        bindir = out / "binaries"
        subprocess.run([str(HERE / "build_rust_binaries.sh"), *(["--no-fetch"] if args.no_fetch else []), str(bindir)],
                       check=True, env={**os.environ, "SOURCE_DATE_EPOCH": str(epoch)})
    prebuilt = verify_prebuilt(bindir, commit, args.allow_dirty)      # the fresh build is held to the same checks
    wheels_dir = out / "wheels"
    arches = args.arch or ["x86_64", "aarch64"]
    artefacts = []
    for arch in arches:
        binary = bindir / scheme.BINARY_NAMES[arch]
        if binary.name not in prebuilt["listed"]:
            raise SystemExit(f"{binary.name} is not listed in {bindir}/SHA256SUMS")
        artefacts.append(artefact(binary, "binary", backend="rust", arch=arch))
        wheel = build_wheels.build_wheel("rust", args.version, arch, binary, wheels_dir, epoch)
        artefacts.append(artefact(wheel, "wheel", backend="rust", arch=arch, tag=tag))

    rust_wheels = {a["arch"]: a for a in artefacts if a["kind"] == "wheel" and a["backend"] == "rust"}
    manifest = {
        "dry_run": True,
        "uploaded": False,
        "tag_created": False,
        "pin_changed": False,
        "commit": commit,
        "tree_dirty_in_transport_rs_or_packaging": dirty,
        "source_date_epoch": epoch,
        "rust_version": args.version,
        "would_create_tag": tag,
        "taken_transport_versions": sorted(taken, key=scheme.Version.parse),
        "binaries_sha256sums": prebuilt["sha256sums"],
        "binaries_build_info": prebuilt["build_info"],
        "artefacts": artefacts,
        "would_upload": [{"tag": a["tag"], "file": a["file"], "url": scheme.asset_url(args.base_url, a["tag"], a["file"])}
                         for a in artefacts if a["kind"] == "wheel"],
        "pin_change_not_applied": pin_snippet(args.base_url, tag, args.version, rust_wheels),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out / "SHA256SUMS").write_text("".join(f"{a['sha256']}  {a['file']}\n" for a in artefacts))
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", required=True, help="the Rust wheel version, 0.7.x (or 0.7.xrcN)")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--binaries", type=Path, help="prebuilt binaries (skips the Docker cross-build)")
    ap.add_argument("--no-fetch", action="store_true", help="pass --no-fetch to the cross-build (offline host)")
    ap.add_argument("--arch", action="append", choices=sorted(scheme.PLATFORM_TAGS))
    ap.add_argument("--base-url", default=os.environ.get(BASE_URL_ENV),
                    help=f"the repository's web root the assets would be uploaded under (or env {BASE_URL_ENV}); no default")
    ap.add_argument("--allow-dirty", action="store_true", help="accept uncommitted changes in transport-rs/ or packaging/rust/")
    args = ap.parse_args(argv)
    m = run(args)
    for a in m["artefacts"]:
        print(f"{a['sha256']}  {a['size']:>9}  {a['file']}")
    print(f"\nDRY RUN of {m['would_create_tag']}: manifest at {args.out / 'manifest.json'}")
    print("NOT done: upload, tag creation, tag push, door/wheel.py pin change, any install. (the owner's sitting.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
