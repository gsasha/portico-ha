#!/usr/bin/env python3
"""Build the platform wheels of `portico_transport` from prebuilt binaries (DEV-764 Phase 4). Stdlib only, no network.

    build_wheels.py --backend rust --version 0.7.0 --binaries OUT_DIR --out WHEELS_DIR [--arch x86_64 --arch aarch64]

`--binaries` is a directory holding `portico-transport-linux-amd64` / `-arm64` / `-armv7` (the file names
`build_rust_binaries.sh` writes and the old Go wheels used). One wheel per architecture, each carrying ONLY its own binary
and a `portico_transport/__init__.py` with the `binary_path()` contract `door/hub.py` calls (the one the deleted Go wheel builder
`packaging/real/` had), plus `BACKEND = "rust"` so a log line or a diagnostic can tell the lineages apart.

Why not `python -m build`, as the Go script does: the wheel is a zip of three small text files and a binary; writing it
directly needs no setuptools, no `pip install build` (network) and no container, and makes the bytes reproducible
(sorted entries, a fixed timestamp from SOURCE_DATE_EPOCH, fixed permissions). The Go and Rust wheels have the same
distribution name (`portico-transport`), so only ONE of them can be installed at a time: that is the point, the
installed lineage is whichever wheel `door/wheel.py` last installed (see release_scheme.py for the rollback).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import stat
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_scheme as scheme  # noqa: E402

DIST = "portico_transport"
SUMMARY = {
    "rust": "Portico's bundled Rust transport binary (SIP/RTP/WebRTC), static musl build",
}

INIT_TEMPLATE = '''"""Locates the portico-transport binary bundled in this platform-tagged wheel.

BACKEND says which lineage built it ("rust"). Each wheel bundles ONLY its own platform's binary: binary_path()
picks the file name for the running platform and confirms it is present, rather than returning a path that does not
exist. The contract the hub (door/hub.py) relies on.
"""
import os
import platform

BACKEND = "%(backend)s"

_BINARY_NAMES = {
    ("Linux", "x86_64"): "portico-transport-linux-amd64",
    ("Linux", "aarch64"): "portico-transport-linux-arm64",
    ("Linux", "armv7l"): "portico-transport-linux-armv7",
}


def binary_path() -> str:
    key = (platform.system(), platform.machine())
    name = _BINARY_NAMES.get(key)
    if name is None:
        raise RuntimeError(f"portico_transport: no bundled binary for platform {key!r}")
    path = os.path.join(os.path.dirname(__file__), name)
    if not os.path.isfile(path):
        raise RuntimeError(f"portico_transport: expected binary not found at {path}")
    return path
'''


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _date_time(epoch: int) -> tuple[int, int, int, int, int, int]:
    import time
    t = time.gmtime(max(epoch, 315532800))      # zip cannot represent before 1980
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec - t.tm_sec % 2)


def wheel_files(backend: str, version: str, arch: str, binary: bytes) -> list[tuple[str, bytes, int]]:
    """(name, data, unix mode) for every file except RECORD, in the order they are written."""
    scheme.Version.parse(version)
    di = f"{DIST}-{version}.dist-info"
    metadata = (
        "Metadata-Version: 2.1\n"
        "Name: portico-transport\n"
        f"Version: {version}\n"
        f"Summary: {SUMMARY[backend]}\n"
        "Requires-Python: >=3.11\n"
    )
    wheel_meta = (
        "Wheel-Version: 1.0\n"
        "Generator: portico-packaging (build_wheels.py)\n"
        "Root-Is-Purelib: false\n"
        f"Tag: py3-none-{scheme.PLATFORM_TAGS[arch]}\n"
    )
    return [
        (f"{DIST}/__init__.py", (INIT_TEMPLATE % {"backend": backend}).encode(), 0o644),
        (f"{DIST}/{scheme.BINARY_NAMES[arch]}", binary, 0o755),
        (f"{di}/METADATA", metadata.encode(), 0o644),
        (f"{di}/WHEEL", wheel_meta.encode(), 0o644),
        (f"{di}/top_level.txt", f"{DIST}\n".encode(), 0o644),
    ]


def build_wheel(backend: str, version: str, arch: str, binary_file: Path, out_dir: Path, epoch: int) -> Path:
    if backend != "rust":
        raise scheme.SchemeError(f"unknown backend {backend!r}")
    if backend == "rust":
        scheme.tag_for("rust", version)                 # refuses a version outside 0.7.x
    data = binary_file.read_bytes()
    if not data.startswith(b"\x7fELF"):
        raise scheme.SchemeError(f"{binary_file} is not an ELF binary")
    files = wheel_files(backend, version, arch, data)
    di = f"{DIST}-{version}.dist-info"
    record = "".join(f"{name},{_record_hash(content)},{len(content)}\n" for name, content, _ in files)
    record += f"{di}/RECORD,,\n"
    files.append((f"{di}/RECORD", record.encode(), 0o644))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / scheme.wheel_filename(version, arch)
    stamp = _date_time(epoch)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, content, mode in files:
            info = zipfile.ZipInfo(name, stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.create_system = 3
            z.writestr(info, content, compresslevel=9)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--backend", required=True, choices=["rust"])
    ap.add_argument("--version", required=True)
    ap.add_argument("--binaries", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--arch", action="append", choices=sorted(scheme.PLATFORM_TAGS),
                    help="repeatable; default x86_64 and aarch64")
    ap.add_argument("--source-date-epoch", type=int, default=int(os.environ.get("SOURCE_DATE_EPOCH", "315532800")))
    args = ap.parse_args(argv)
    built = []
    for arch in args.arch or ["x86_64", "aarch64"]:
        binary = args.binaries / scheme.BINARY_NAMES[arch]
        if not binary.is_file():
            ap.error(f"missing {binary}")
        built.append(build_wheel(args.backend, args.version, arch, binary, args.out, args.source_date_epoch))
    for p in built:
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
