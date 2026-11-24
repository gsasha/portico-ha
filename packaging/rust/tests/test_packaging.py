"""Offline tests of the Rust packaging scripts (DEV-764 Phase 4). Stdlib + pytest; no Docker, no network, no Home Assistant.

    python3 -m pytest packaging/rust/tests

The Home Assistant side (the REAL `async_ensure_wheel`) is tested in custom_components/portico/tests/test_wheel.py.
"""
from __future__ import annotations

import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest

import build_wheels
import release_dryrun
import release_scheme as scheme

HERE = Path(__file__).resolve().parent.parent
REPO = HERE.parent.parent
ELF = b"\x7fELF" + b"\x02\x01\x01" + bytes(range(60))


def _binaries(tmp_path: Path, arches=("x86_64", "aarch64"), payload=ELF) -> Path:
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    for a in arches:
        (d / scheme.BINARY_NAMES[a]).write_bytes(payload + a.encode())
    return d


# --- the scheme ------------------------------------------------------------------------------------------------------

def test_the_two_tag_families_do_not_overlap():
    assert scheme.tag_for("go", "0.6.10") == "transport-v0.6.10"
    assert scheme.tag_for("rust", "0.7.0") == "transport-rs-v0.7.0"
    assert not fnmatch.fnmatch("transport-rs-v0.7.0", "transport-v*")       # Go tooling never sees a Rust release
    assert not fnmatch.fnmatch("transport-v0.7.1", "transport-rs-v*")
    assert scheme.parse_tag("transport-rs-v0.7.0rc2") == ("rust", "0.7.0rc2")
    assert scheme.parse_tag("transport-v0.6.10") == ("go", "0.6.10")
    for bad in ("transport-v0.7", "portico-v0.3.0", "transport-rs-v1", "transport-vX.Y.Z"):
        with pytest.raises(scheme.SchemeError):
            scheme.parse_tag(bad)


def test_rust_versions_stay_in_0_7():
    for ok in ("0.7.0", "0.7.12", "0.7.0rc1", "0.10.0"):         # 0.10.x: the line that resumes after a rollback to 0.9.x
        scheme.tag_for("rust", ok)
    for bad in ("0.6.11", "0.8.0", "0.9.0", "1.0.0", "0.7", "0.7.0.post1", "v0.7.0"):
        with pytest.raises(scheme.SchemeError):
            scheme.tag_for("rust", bad)


def test_version_order_is_pep440_order():
    """The installer compares with packaging.Version; this module's own order must agree on every pair."""
    from packaging.version import Version as P
    versions = ["0.6.4", "0.6.10", "0.7.0rc1", "0.7.0rc2", "0.7.0", "0.7.1", "0.7.10", "0.8.0"]
    for a in versions:
        for b in versions:
            assert (scheme.Version.parse(a) < scheme.Version.parse(b)) == (P(a) < P(b)), (a, b)
            assert (scheme.Version.parse(a) <= scheme.Version.parse(b)) == (P(a) <= P(b)), (a, b)


def test_next_version():
    assert scheme.next_version(["0.6.10"], "rust") == "0.7.0"           # first Rust release after Go 0.6.x
    assert scheme.next_version(["0.6.10", "0.7.0rc1"], "rust") == "0.7.0"
    assert scheme.next_version(["0.6.10", "0.7.0"], "rust") == "0.7.1"
    assert scheme.next_version(["0.7.2", "0.7.3"], "go") == "0.7.4"
    assert scheme.next_version([], "rust") == "0.7.0"
    # an old Go rollback tag (0.9.0) in the list does not stop Rust 0.7.x fixes
    assert scheme.next_version(["0.6.10", "0.7.0", "0.9.0"], "rust") == "0.7.1"


def test_wheel_file_names_and_asset_urls_match_the_shape_of_the_current_pin():
    assert scheme.wheel_filename("0.7.0", "x86_64") == "portico_transport-0.7.0-py3-none-linux_x86_64.whl"
    # aarch64 is the plain linux tag, NOT manylinux2014 (pip on musl, i.e. Home Assistant OS, refuses manylinux)
    assert scheme.wheel_filename("0.7.0", "aarch64") == "portico_transport-0.7.0-py3-none-linux_aarch64.whl"
    url = scheme.asset_url("http://h:3000/o/r/", "transport-rs-v0.7.0", scheme.wheel_filename("0.7.0", "x86_64"))
    assert url == "http://h:3000/o/r/releases/download/transport-rs-v0.7.0/portico_transport-0.7.0-py3-none-linux_x86_64.whl"
    # the live pin (read from the literal WHEEL_PIN_URLS of door/wheel.py, per architecture, either tag family) has the same shape
    import ast
    tree = ast.parse((REPO / "custom_components/portico/door/wheel.py").read_text())
    urls = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "WHEEL_PIN_URLS" for t in n.targets))
    assert "x86_64" in urls
    for arch, pin_url in urls.items():
        pin = re.fullmatch(r"(https?://[^#]+/releases/download/)(transport(?:-rs)?-v)([\d.]+)/portico_transport-([\d.]+)-py3-none-linux_(\w+)\.whl(?:#sha256=[0-9a-f]{64})?", pin_url)
        assert pin and pin.group(1) == "https://github.com/gsasha/portico-ha/releases/download/", pin_url
        assert pin.group(3) == pin.group(4) and pin.group(5) == arch
    assert not hasattr(release_dryrun, "DEFAULT_BASE_URL")               # R1-49: no private address default in the source


# --- the wheel -------------------------------------------------------------------------------------------------------

def test_a_wheel_has_its_own_binary_only_a_valid_record_and_an_executable_bit(tmp_path):
    bins = _binaries(tmp_path)
    path = build_wheels.build_wheel("rust", "0.7.0", "aarch64", bins / "portico-transport-linux-arm64", tmp_path / "w", 1_700_000_000)
    assert path.name == "portico_transport-0.7.0-py3-none-linux_aarch64.whl"
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        assert sorted(names) == sorted([
            "portico_transport/__init__.py", "portico_transport/portico-transport-linux-arm64",
            "portico_transport-0.7.0.dist-info/METADATA", "portico_transport-0.7.0.dist-info/WHEEL",
            "portico_transport-0.7.0.dist-info/top_level.txt", "portico_transport-0.7.0.dist-info/RECORD"])
        assert not any("amd64" in n for n in names)                     # each wheel carries only its own platform
        mode = z.getinfo("portico_transport/portico-transport-linux-arm64").external_attr >> 16
        assert stat.S_IMODE(mode) == 0o755 and stat.S_ISREG(mode)
        assert z.read("portico_transport-0.7.0.dist-info/WHEEL").decode().splitlines()[-1] == "Tag: py3-none-linux_aarch64"
        meta = z.read("portico_transport-0.7.0.dist-info/METADATA").decode()
        assert "Name: portico-transport\n" in meta and "Version: 0.7.0\n" in meta and "Rust" in meta
        # RECORD: every file but itself, hashes and sizes right (the format pip verifies on install)
        import base64
        rows = [r.split(",") for r in z.read("portico_transport-0.7.0.dist-info/RECORD").decode().splitlines()]
        assert {r[0] for r in rows} == set(names)
        for name, digest, size in rows:
            if name.endswith("RECORD"):
                assert digest == "" and size == ""
                continue
            data = z.read(name)
            want = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            assert (digest, int(size)) == (want, len(data)), name
        assert z.testzip() is None


def test_the_same_inputs_give_the_same_bytes(tmp_path):
    bins = _binaries(tmp_path)
    a = build_wheels.build_wheel("rust", "0.7.0", "x86_64", bins / "portico-transport-linux-amd64", tmp_path / "a", 1_700_000_001)
    b = build_wheels.build_wheel("rust", "0.7.0", "x86_64", bins / "portico-transport-linux-amd64", tmp_path / "b", 1_700_000_001)
    assert a.read_bytes() == b.read_bytes()
    c = build_wheels.build_wheel("rust", "0.7.0", "x86_64", bins / "portico-transport-linux-amd64", tmp_path / "c", 1_700_000_003)
    assert c.read_bytes() != a.read_bytes()                             # the timestamp is the only varying input


def test_refusals(tmp_path):
    bins = _binaries(tmp_path)
    amd = bins / "portico-transport-linux-amd64"
    with pytest.raises(scheme.SchemeError):                              # a Rust wheel outside 0.7.x
        build_wheels.build_wheel("rust", "0.6.11", "x86_64", amd, tmp_path / "w", 0)
    with pytest.raises(scheme.SchemeError):                              # not a binary (a stale path, a text file)
        (tmp_path / "junk").write_text("hello")
        build_wheels.build_wheel("rust", "0.7.0", "x86_64", tmp_path / "junk", tmp_path / "w", 0)
    with pytest.raises(scheme.SchemeError):
        build_wheels.build_wheel("zig", "0.7.0", "x86_64", amd, tmp_path / "w", 0)


def test_binary_path_in_the_wheel_finds_the_binary_for_the_running_platform(tmp_path, monkeypatch):
    """Extract a built wheel and call its own `binary_path()` for both architectures (the hub's only call)."""
    import platform
    bins = _binaries(tmp_path)
    for arch, machine, name in (("x86_64", "x86_64", "amd64"), ("aarch64", "aarch64", "arm64")):
        wheel = build_wheels.build_wheel("rust", "0.7.0", arch, bins / f"portico-transport-linux-{name}", tmp_path / "w", 0)
        dest = tmp_path / f"x-{arch}"
        zipfile.ZipFile(wheel).extractall(dest)
        spec = importlib.util.spec_from_file_location("pt", dest / "portico_transport/__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.BACKEND == "rust"
        monkeypatch.setattr(platform, "machine", lambda m=machine: m)
        assert mod.binary_path() == str(dest / "portico_transport" / f"portico-transport-linux-{name}")
    monkeypatch.setattr(platform, "machine", lambda: "riscv64")
    with pytest.raises(RuntimeError):
        mod.binary_path()


def test_the_init_template_has_the_binary_path_the_hub_calls():
    """The hub (door/hub.py) calls portico_transport.binary_path(); the template must keep defining it."""
    assert "def binary_path" in build_wheels.INIT_TEMPLATE
    assert 'BACKEND = "%(backend)s"' in build_wheels.INIT_TEMPLATE


# --- the cross-build script, with a stub docker ----------------------------------------------------------------------

STUB = r'''#!/usr/bin/env python3
import os, re, sys
log = os.environ["STUB_LOG"]
args = sys.argv[1:]
with open(log, "a") as f:
    f.write(" ".join(args) + "\n")
if args[:2] == ["image", "inspect"]:
    sys.exit(1 if os.environ.get("STUB_NO_IMAGE") else 0)
if args[0] == "run" and "readelf" in args:
    # readelf "inside the image": the file lives under the mounted target dir. A real ELF (the dynamic-binary test) goes to
    # the host's readelf; the placeholder ELF the stub writes for the other tests is "static" (no INTERP, no NEEDED).
    tdir = next(a for a in args if a.endswith(":/cache/target")).split(":")[0]
    path = tdir + args[-1][len("/cache/target"):]
    data = open(path, "rb").read()
    if not data.startswith(b"\x7fELF"):
        sys.stderr.write("readelf: Error: Not an ELF file\n")
        sys.exit(1)
    if len(data) > 1000:
        os.execvp("readelf", ["readelf", *args[args.index("readelf") + 1:-1], path])
    print("There is no dynamic section in this file." if "-d" in args else "Program Headers: (static)")
    sys.exit(0)
if args[0] == "run" and "cargo" in args and "build" in args:
    triple = args[args.index("--target") + 1]
    tdir = next(a for a in args if a.endswith(":/cache/target")).split(":")[0]
    os.makedirs(f"{tdir}/{triple}/release", exist_ok=True)
    with open(f"{tdir}/{triple}/release/portico-transport", "wb") as f:
        if os.environ.get("STUB_BINARY"):
            f.write(open(os.environ["STUB_BINARY"], "rb").read())
        else:
            f.write(os.environ.get("STUB_PREFIX", "\x7fELF-").encode("latin-1") + triple.encode())
sys.exit(0)
'''


def _run_build(tmp_path, *flags, no_image=False, extra_env=None):
    stub = tmp_path / "docker"
    stub.write_text(STUB)
    stub.chmod(0o755)
    log = tmp_path / "docker.log"
    # The caller's PORTICO_RS_* overrides are dropped: with them set, the stub would write into a REAL target dir.
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("PORTICO_RS_")},
           "DOCKER": str(stub), "STUB_LOG": str(log), "PORTICO_RS_CACHE": str(tmp_path / "cache"),
           "SOURCE_DATE_EPOCH": "1700000000", **(extra_env or {})}
    if no_image:
        env["STUB_NO_IMAGE"] = "1"
    out = tmp_path / "out"
    cp = subprocess.run([str(HERE / "build_rust_binaries.sh"), *flags, str(out)], capture_output=True, text=True, env=env)
    return cp, out, log.read_text().splitlines() if log.exists() else []


def test_the_cross_build_compiles_both_targets_offline_locked_and_read_only(tmp_path):
    cp, out, calls = _run_build(tmp_path)
    assert cp.returncode == 0, cp.stderr
    builds = [c for c in calls if " cargo build" in c]
    assert len(builds) == 2
    assert [b for b in builds if "--target x86_64-unknown-linux-musl" in b] and [b for b in builds if "--target aarch64-unknown-linux-musl" in b]
    for b in builds:
        assert "--network none" in b                                     # the compile cannot reach the network
        assert "--locked" in b and "--offline" in b and "--release" in b
        assert "/transport-rs:/work:ro" in b                             # the source is mounted read-only
        assert "CARGO_INCREMENTAL=0" in b and "SOURCE_DATE_EPOCH=1700000000" in b
        assert "--remap-path-prefix=/work=/src" in b
        assert f"-u {os.getuid()}:{os.getgid()}" in b                    # nothing root-owned left behind
    fetches = [c for c in calls if " cargo fetch" in c]
    assert len(fetches) == 1 and "--network bridge" in fetches[0] and "--locked" in fetches[0]
    assert sorted(p.name for p in out.iterdir()) == [
        "SHA256SUMS", "build-info.txt", "portico-transport-linux-amd64", "portico-transport-linux-arm64"]
    sums = (out / "SHA256SUMS").read_text().splitlines()
    for line in sums:
        digest, fname = line.split("  ")
        assert hashlib.sha256((out / fname).read_bytes()).hexdigest() == digest
    assert (out / "portico-transport-linux-arm64").read_bytes().endswith(b"aarch64-unknown-linux-musl")
    info = (out / "build-info.txt").read_text()
    assert "source_date_epoch=1700000000" in info and re.search(r"^commit=[0-9a-f]{40}$", info, re.M)
    # the linker for aarch64 is the cross gcc; x86_64 keeps rustc's self-contained default (as scripts/build_musl.sh)
    assert any("CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_LINKER=aarch64-linux-gnu-gcc" in b for b in builds)
    assert not any("CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER" in b for b in builds)    # musl-gcc as linker gave a PT_INTERP


def test_no_fetch_and_one_arch_and_a_missing_image(tmp_path):
    cp, out, calls = _run_build(tmp_path, "--no-fetch", "--arch", "aarch64", no_image=True)
    assert cp.returncode == 0, cp.stderr
    assert not [c for c in calls if " cargo fetch" in c]                 # offline host: no network step at all
    assert [c for c in calls if c.startswith("build ") and "Dockerfile.build" in c]       # the image is built when absent
    assert len([c for c in calls if " cargo build" in c]) == 1
    assert (out / "portico-transport-linux-arm64").exists() and not (out / "portico-transport-linux-amd64").exists()


def test_the_cross_build_rejects_bad_arguments(tmp_path):
    for flags in (["--arch", "armv7"], ["--bogus"]):
        cp, _, calls = _run_build(tmp_path, *flags)
        assert cp.returncode == 2 and not [c for c in calls if " cargo build" in c]


def _sub(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def test_readelf_runs_inside_the_image_and_its_failure_fails_the_build(tmp_path):
    cp, out, calls = _run_build(tmp_path, "--arch", "x86_64")
    assert cp.returncode == 0, cp.stderr
    reads = [c for c in calls if " readelf " in c]
    assert len(reads) == 2 and all("local/portico-rs-build:3" in c and "--network none" in c for c in reads)
    assert any("readelf -lW /cache/target/x86_64-unknown-linux-musl/release/portico-transport" in c for c in reads)
    assert any("readelf -d /cache/target/x86_64-unknown-linux-musl/release/portico-transport" in c for c in reads)
    # R1-36: not an ELF at all -> readelf exits 1 -> the build fails (it used to "pass" both greps)
    cp, out, _ = _run_build(_sub(tmp_path, "n"), "--arch", "x86_64", extra_env={"STUB_PREFIX": "not-an-elf-"})
    assert cp.returncode == 1 and "readelf could not read" in cp.stderr
    assert not (out / "portico-transport-linux-amd64").exists() and not (out / "SHA256SUMS").exists()


def test_a_dynamically_linked_binary_fails_the_static_check(tmp_path):
    if not shutil.which("readelf"):
        pytest.skip("the stub hands a real ELF to the host's readelf")
    dynamic = shutil.which("true")                                       # a dynamically linked ELF on any Linux host
    assert dynamic and subprocess.run(["readelf", "-lW", dynamic], capture_output=True, text=True).stdout.count("INTERP")
    cp, out, _ = _run_build(tmp_path, "--arch", "x86_64", extra_env={"STUB_BINARY": str(Path(dynamic).resolve())})
    assert cp.returncode == 1 and "not fully static" in cp.stderr
    assert not (out / "portico-transport-linux-amd64").exists()


def test_the_build_image_is_pinned(tmp_path):
    """R1-48: base image by version AND digest, apt packages by version, a toolchain file with the same rustc."""
    df = (HERE / "Dockerfile.build").read_text()
    m = re.search(r"^ARG RUST_IMAGE=rust:(\d+\.\d+\.\d+)-bookworm@sha256:([0-9a-f]{64})$", df, re.M)
    assert m, "the base image must be rust:<exact version>-bookworm@sha256:<digest>"
    for pkg in ("musl-tools", "gcc-aarch64-linux-gnu", "libc6-dev-arm64-cross"):
        assert re.search(rf"\b{re.escape(pkg)}=\S+", df), pkg
    toolchain = (REPO / "transport-rs/rust-toolchain.toml").read_text()
    assert f'channel = "{m.group(1)}"' in toolchain
    tag = re.search(r"-t (local/portico-rs-build:\d+)", df).group(1)
    assert f"IMAGE=${{PORTICO_RS_IMAGE:-{tag}}}" in (HERE / "build_rust_binaries.sh").read_text()   # the tag is bumped together


# --- the committed double build (R1-141) -----------------------------------------------------------------------------

def test_the_readme_carries_a_double_build_result_that_matches_the_pins():
    """The reproducibility claim is evidence in the tree: two SHA256SUMS of two independent builds, identical, with the image
    and rustc they were made with. The build itself is test_a_real_double_build_is_byte_identical (opt-in)."""
    readme = (HERE / "README.md").read_text()
    sec = readme.split("### Double-build result", 1)
    assert len(sec) == 2, "README.md needs a '### Double-build result' section"
    body = sec[1].split("\n## ", 1)[0]
    runs = re.findall(r"^Run ([12]):\n```\n(.*?)```", body, re.M | re.S)
    assert [r[0] for r in runs] == ["1", "2"]
    assert runs[0][1] == runs[1][1] and re.search(r"^[0-9a-f]{64}  portico-transport-linux-amd64$", runs[0][1], re.M)
    assert re.search(r"^[0-9a-f]{64}  portico-transport-linux-arm64$", runs[0][1], re.M)
    df = (HERE / "Dockerfile.build").read_text()
    assert re.search(r"^ARG RUST_IMAGE=(\S+)$", df, re.M).group(1) in body            # the digest the result was made with
    assert re.search(r"rustc \d+\.\d+\.\d+ \(", body)


@pytest.mark.skipif(os.environ.get("PORTICO_RS_REAL_BUILD") != "1", reason="opt-in: PORTICO_RS_REAL_BUILD=1 (Docker, ~10 min per arch)")
def test_a_real_double_build_is_byte_identical(tmp_path):
    """Builds both targets twice with separate target dirs (same cargo home, which needs a warm cache or PORTICO_RS_FETCH=1)
    and compares the two SHA256SUMS. Environment: PORTICO_RS_CACHE for the cargo home / default cache root."""
    fetch = [] if os.environ.get("PORTICO_RS_FETCH") == "1" else ["--no-fetch"]
    sums = []
    for n in (1, 2):
        env = {**os.environ, "PORTICO_RS_TARGET_DIR": str(tmp_path / f"target{n}")}
        cp = subprocess.run([str(HERE / "build_rust_binaries.sh"), *fetch, str(tmp_path / f"out{n}")],
                            capture_output=True, text=True, env=env)
        assert cp.returncode == 0, cp.stdout[-2000:] + cp.stderr[-2000:]
        sums.append((tmp_path / f"out{n}" / "SHA256SUMS").read_text())
    assert sums[0] == sums[1] and sums[0].count("\n") == 2


# --- the dry run -----------------------------------------------------------------------------------------------------

def _git_repo(tmp_path: Path, tags=()) -> Path:
    repo = tmp_path / "repo"
    (repo / "transport-rs").mkdir(parents=True)
    (repo / "transport-rs/a.txt").write_text("x")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z"}
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "c"], *[["tag", t] for t in tags]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env, capture_output=True)
    return repo


TEST_BASE = "http://forge.example:3000/owner/repo"


def _seal(bindir: Path, repo: Path, transport_rs="clean", commit=None) -> Path:
    """Write the SHA256SUMS and build-info.txt that build_rust_binaries.sh writes next to the binaries."""
    commit = commit or subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    names = sorted(p.name for p in bindir.iterdir() if p.name.startswith("portico-transport-linux-"))
    (bindir / "SHA256SUMS").write_text("".join(f"{hashlib.sha256((bindir / n).read_bytes()).hexdigest()}  {n}\n" for n in names))
    (bindir / "build-info.txt").write_text(f"commit={commit}\ntransport-rs={transport_rs}\nsource_date_epoch=1\nimage=x\nimage_id=y\nrustc=rustc 1.99.1 (x)\n")
    return bindir


def _args(tmp_path, repo, **kw):
    base = dict(version="0.7.0", out=tmp_path / "rel", binaries=None, no_fetch=False, arch=None,
                base_url=TEST_BASE, allow_dirty=False)
    base.update(kw)
    if base["binaries"] is None:
        base["binaries"] = _seal(_binaries(tmp_path), repo)
    return Namespace(**base)


def test_the_dry_run_writes_a_manifest_and_changes_nothing(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, tags=["transport-v0.6.10", "transport-v0.6.9"])
    monkeypatch.setattr(release_dryrun, "REPO", repo)
    before = subprocess.run(["git", "-C", str(repo), "for-each-ref"], capture_output=True, text=True).stdout
    m = release_dryrun.run(_args(tmp_path, repo))
    assert (m["dry_run"], m["uploaded"], m["tag_created"], m["pin_changed"]) == (True, False, False, False)
    assert m["would_create_tag"] == "transport-rs-v0.7.0"
    assert "go_rollback" not in m
    assert m["taken_transport_versions"] == ["0.6.9", "0.6.10"]
    pin = m["pin_change_not_applied"]
    assert pin["applied"] is False and pin["WHEEL_PIN_VERSION"] == "0.7.0"
    x86 = next(a for a in m["artefacts"] if a["kind"] == "wheel" and a["backend"] == "rust" and a["arch"] == "x86_64")
    assert pin["WHEEL_PIN_SHA256"] == x86["sha256"]                      # R1-49: the pin carries the wheel's hash
    arm = next(a for a in m["artefacts"] if a["kind"] == "wheel" and a["backend"] == "rust" and a["arch"] == "aarch64")
    assert pin["WHEEL_PIN_URLS"] == {                                    # per architecture: each host gets its own wheel
        "x86_64": f"{TEST_BASE}/releases/download/transport-rs-v0.7.0/portico_transport-0.7.0-py3-none-linux_x86_64.whl#sha256={x86['sha256']}",
        "aarch64": f"{TEST_BASE}/releases/download/transport-rs-v0.7.0/portico_transport-0.7.0-py3-none-linux_aarch64.whl#sha256={arm['sha256']}"}
    assert pin["WHEEL_PIN_SHA256S"] == {"x86_64": x86["sha256"], "aarch64": arm["sha256"]}
    assert pin["WHEEL_PIN_URL"] == (TEST_BASE + "/releases/download/transport-rs-v0.7.0/"
                                    "portico_transport-0.7.0-py3-none-linux_x86_64.whl#sha256=" + x86["sha256"])
    assert m["binaries_sha256sums"] == (tmp_path / "bin/SHA256SUMS").read_text()          # R1-35: both files are in the manifest
    assert m["binaries_build_info"] == (tmp_path / "bin/build-info.txt").read_text()
    on_disk = json.loads((tmp_path / "rel/manifest.json").read_text())
    assert on_disk == json.loads(json.dumps(m))
    kinds = sorted((a["backend"], a["kind"], a["arch"]) for a in m["artefacts"])
    assert kinds == sorted([(b, k, a) for b in ("rust",) for k in ("binary", "wheel") for a in ("x86_64", "aarch64")]
                           )
    for a in m["artefacts"]:                                             # sizes and sums are of the files on disk
        p = next((tmp_path / "rel").rglob(a["file"])) if a["kind"] == "wheel" else tmp_path / "bin" / a["file"]
        assert p.stat().st_size == a["size"] and hashlib.sha256(p.read_bytes()).hexdigest() == a["sha256"]
    assert {u["tag"] for u in m["would_upload"]} == {"transport-rs-v0.7.0"}
    sums = (tmp_path / "rel/SHA256SUMS").read_text()
    assert all(a["sha256"] in sums for a in m["artefacts"])
    after = subprocess.run(["git", "-C", str(repo), "for-each-ref"], capture_output=True, text=True).stdout
    assert before == after                                               # no tag, no ref created
    wheel_py = (REPO / "custom_components/portico/door/wheel.py").read_text()
    assert 'WHEEL_PIN_VERSION = "0.7.0"' not in wheel_py                 # the pin file is untouched


def _mod(tmp_path, monkeypatch, **seal):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(release_dryrun, "REPO", repo)
    return repo


def test_a_dirty_tree_is_refused_unless_allowed(tmp_path, monkeypatch):
    repo = _mod(tmp_path, monkeypatch)
    args = _args(tmp_path, repo)
    (repo / "transport-rs/a.txt").write_text("changed")
    with pytest.raises(SystemExit, match="uncommitted"):
        release_dryrun.run(args)
    assert not (tmp_path / "rel").exists()                               # refused before anything was written
    assert release_dryrun.run(_args(tmp_path, repo, allow_dirty=True))["tree_dirty_in_transport_rs_or_packaging"] is True


@pytest.mark.parametrize("what, match", [
    ("no_sums", "SHA256SUMS is missing"),
    ("no_info", "build-info.txt is missing"),
    ("tampered", "SHA256SUMS mismatch"),
    ("other_commit", "HEAD is"),
    ("dirty_build", "dirty tree"),
    ("unlisted", "not listed"),
])
def test_prebuilt_binaries_need_their_sums_and_a_clean_build_of_head(tmp_path, monkeypatch, what, match):
    repo = _mod(tmp_path, monkeypatch)
    bins = _binaries(tmp_path)
    kw = {}
    if what == "dirty_build":
        kw["transport_rs"] = "DIRTY"
    if what == "other_commit":
        kw["commit"] = "0" * 40
    _seal(bins, repo, **kw)
    if what == "no_sums":
        (bins / "SHA256SUMS").unlink()
    if what == "no_info":
        (bins / "build-info.txt").unlink()
    if what == "tampered":
        (bins / "portico-transport-linux-arm64").write_bytes(ELF + b"swapped")
    if what == "unlisted":
        lines = (bins / "SHA256SUMS").read_text().splitlines()
        (bins / "SHA256SUMS").write_text("\n".join(l for l in lines if "arm64" not in l) + "\n")
    with pytest.raises(SystemExit, match=match):
        release_dryrun.run(_args(tmp_path, repo, binaries=bins))
    assert not (tmp_path / "rel" / "manifest.json").exists()


def test_a_dirty_build_is_accepted_only_with_allow_dirty(tmp_path, monkeypatch):
    repo = _mod(tmp_path, monkeypatch)
    bins = _seal(_binaries(tmp_path), repo, transport_rs="DIRTY")
    assert release_dryrun.run(_args(tmp_path, repo, binaries=bins, allow_dirty=True))["binaries_build_info"].count("DIRTY") == 1


def test_there_is_no_default_release_url_in_the_source(tmp_path, monkeypatch, capsys):
    """R1-49: no private address in the source (Apache-2.0 publication); the URL comes from --base-url or the environment."""
    assert not re.search(r"\b10\.\d+\.\d+\.\d+\b", (HERE / "release_dryrun.py").read_text())
    repo = _mod(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="base URL"):
        release_dryrun.run(_args(tmp_path, repo, base_url=None))
    monkeypatch.delenv("PORTICO_RELEASE_BASE_URL", raising=False)
    monkeypatch.setenv("PORTICO_RELEASE_BASE_URL", "http://env.example/o/r")
    monkeypatch.setattr(release_dryrun, "run", lambda a: pytest.fail("not reached") if a.base_url != "http://env.example/o/r" else
                        {"artefacts": [], "would_create_tag": "t"})
    assert release_dryrun.main(["--version", "0.7.0", "--out", str(tmp_path / "o")]) == 0


def test_a_version_that_already_has_a_tag_is_refused(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path, tags=["transport-rs-v0.7.0"])
    monkeypatch.setattr(release_dryrun, "REPO", repo)
    with pytest.raises(SystemExit, match="already exists"):
        release_dryrun.run(_args(tmp_path, repo))


def test_the_dry_run_can_only_run_read_only_git():
    for bad in (["push", "origin", "x"], ["tag", "transport-rs-v0.7.0"], ["tag", "-a", "x"], ["commit", "-m", "x"], ["reset", "--hard"], []):
        with pytest.raises(RuntimeError):
            release_dryrun._git(*bad)
    assert re.fullmatch(r"[0-9a-f]{40}", release_dryrun._git("rev-parse", "HEAD"))


def test_the_release_scripts_contain_no_publishing_code():
    for name in ("release_dryrun.py", "build_wheels.py", "release_scheme.py", "build_rust_binaries.sh"):
        src = (HERE / name).read_text()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        for needle in ("git push", "curl ", "wget ", "urllib", "requests", "http.client", "socket", "smtplib", "docker push",
                       "docker login", "gh release", "api/v1", "ssh "):
            assert needle not in code, (name, needle)


# --- the retired Go tag family -----------------------------------------------------------------------------------------

def test_the_old_go_rollback_tag_still_parses_and_names_its_wheel():
    assert scheme.tag_for("go", "0.9.0") == "transport-v0.9.0"
    assert scheme.parse_tag("transport-v0.9.0") == ("go", "0.9.0")
    assert scheme.wheel_filename("0.9.0", "aarch64") == "portico_transport-0.9.0-py3-none-linux_aarch64.whl"
