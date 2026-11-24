"""The release tag and version scheme of the two transport lineages (DEV-764 Phase 4). Pure functions, stdlib only.

One wheel name, `portico_transport`, two lineages, ONE monotonic version line:

    transport-v<X.Y.Z>      the Go transport (existing family, last 0.6.10); wheel portico_transport-<X.Y.Z>
    transport-rs-v<X.Y.Z>   the Rust transport (new family, versions 0.7.x);  wheel portico_transport-<X.Y.Z>

`transport-v*` does not match `transport-rs-v*` (the glob needs a `v` right after `transport-`), so existing tooling
that lists Go releases never sees a Rust one. A release candidate is `X.Y.ZrcN` (tag `transport-rs-v0.7.0rc1`): PEP 440
puts it BELOW 0.7.0, so a candidate never shadows the release.

One version line is shared because `door/wheel.py` installs the pin only when the installed wheel is OLDER than the pin.
The Go family (`transport-v*`, last 0.6.10, plus the never-merged 0.9.0 rollback re-release) is no longer built here: the
Go source and its release tooling were removed after the cut-over (D-20261119-c1af, "we will not roll back to the Go wheel
anymore"). The `go` tag family stays parseable so the existing tags still sort into the version line; the ROLLBACK_MINOR
line stays reserved so a Rust release can never take a number that the old Go tags used.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

RUST_MINOR = (0, 7)             # Rust wheels live in 0.7.x (the plan); anything else is refused
RUST_RESUME_MINOR = 10          # after a ROLLBACK has been pinned (0.9.x installed), the Rust line resumes at 0.10.0: it must beat 0.9.x
ROLLBACK_MINOR = (0, 9)         # the Go ROLLBACK re-release line (D-20261114-6beb, option a): 0.9.0, 0.9.1, ...; above all of 0.7.x
TAG_FAMILIES = {"go": "transport-v", "rust": "transport-rs-v"}
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:rc(\d+))?$")
# What a wheel file name carries: the platform tag per architecture. Both lineages use the same plain `linux_*` tags,
# which pip accepts on glibc and musl alike (the binaries are static); `manylinux2014_aarch64` (what the old Go
# script used for arm64) is refused by pip on a musl system such as Home Assistant OS.
PLATFORM_TAGS = {"x86_64": "linux_x86_64", "aarch64": "linux_aarch64", "armv7": "linux_armv7l"}
BINARY_NAMES = {"x86_64": "portico-transport-linux-amd64", "aarch64": "portico-transport-linux-arm64",
                "armv7": "portico-transport-linux-armv7"}


class SchemeError(ValueError):
    pass


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    rc: int | None = None       # None = the final release

    @classmethod
    def parse(cls, text: str) -> "Version":
        m = _VERSION.match(text)
        if not m:
            raise SchemeError(f"not a release version (X.Y.Z or X.Y.ZrcN): {text!r}")
        rc = int(m.group(4)) if m.group(4) is not None else None
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)), rc)

    def sort_key(self):
        # PEP 440: X.Y.ZrcN < X.Y.Z
        return (self.major, self.minor, self.patch, 1 if self.rc is None else 0, self.rc or 0)

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if self.rc is None else f"{base}rc{self.rc}"

    def __lt__(self, other):
        return self.sort_key() < other.sort_key()

    def __le__(self, other):
        return self.sort_key() <= other.sort_key()

    def __gt__(self, other):
        return self.sort_key() > other.sort_key()

    def __ge__(self, other):
        return self.sort_key() >= other.sort_key()


def tag_for(backend: str, version: str) -> str:
    Version.parse(version)
    if backend not in TAG_FAMILIES:
        raise SchemeError(f"backend must be one of {sorted(TAG_FAMILIES)}, got {backend!r}")
    if backend == "rust":
        v = Version.parse(version)
        if (v.major, v.minor) != RUST_MINOR and not (v.major == 0 and v.minor >= RUST_RESUME_MINOR):
            raise SchemeError(f"Rust wheels are {RUST_MINOR[0]}.{RUST_MINOR[1]}.x (or 0.{RUST_RESUME_MINOR}.x and up, the line that "
                              f"resumes after a rollback to {ROLLBACK_MINOR[0]}.{ROLLBACK_MINOR[1]}.x), got {version}")
    return TAG_FAMILIES[backend] + version


def parse_tag(tag: str) -> tuple[str, str]:
    """('rust'|'go', version). `transport-rs-v` is tried first only for clarity; the prefixes cannot overlap."""
    for backend, prefix in TAG_FAMILIES.items():
        if tag.startswith(prefix):
            Version.parse(tag[len(prefix):])
            return backend, tag[len(prefix):]
    raise SchemeError(f"not a transport release tag: {tag!r}")


def wheel_filename(version: str, arch: str) -> str:
    Version.parse(version)
    if arch not in PLATFORM_TAGS:
        raise SchemeError(f"unknown architecture {arch!r}")
    return f"portico_transport-{version}-py3-none-{PLATFORM_TAGS[arch]}.whl"


def asset_url(base: str, tag: str, filename: str) -> str:
    """The URL a pin would carry, same shape as `WHEEL_PIN_URL` in door/wheel.py. `base` is the repo's web root,
    e.g. https://github.com/gsasha/portico-ha . Computed for the manifest only; nothing is fetched."""
    return f"{base.rstrip('/')}/releases/download/{tag}/{filename}"


def next_version(existing: list[str], backend: str) -> str:
    """The next FINAL version above every version of EITHER lineage in `existing` (finals and candidates). After a
    candidate X.Y.ZrcN the next final is X.Y.Z itself; after a final it is the next patch. Rust stays inside 0.7.x."""
    parsed = [Version.parse(v) for v in existing]
    if backend == "rust":       # the reserved rollback line (0.9.x) sits ABOVE the Rust line and does not advance it: a rollback
        # release that is only STAGED (uploaded, not pinned) must not stop Rust 0.7.x fixes. After a rollback was really
        # pinned, cut the resumed line explicitly (`--version 0.10.0`).
        parsed = [v for v in parsed if (v.major, v.minor) <= RUST_MINOR]
    if not parsed:
        return f"{RUST_MINOR[0]}.{RUST_MINOR[1]}.0" if backend == "rust" else "0.7.0"
    top = max(parsed)
    nxt = Version(top.major, top.minor, top.patch if top.rc is not None else top.patch + 1)
    if backend == "rust" and (nxt.major, nxt.minor) < RUST_MINOR:       # the first Rust release after Go 0.6.x
        return f"{RUST_MINOR[0]}.{RUST_MINOR[1]}.0"
    if backend == "rust" and (nxt.major, nxt.minor) != RUST_MINOR:
        raise SchemeError(f"the next Rust version would be {nxt}, outside {RUST_MINOR[0]}.{RUST_MINOR[1]}.x")
    return str(nxt)
