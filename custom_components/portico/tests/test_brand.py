"""The integration ships its own icon in custom_components/portico/brand/, which HA 2026.9's
brands proxy serves for a custom integration (no brands-repo submission needed). No HA harness
needed: the PNG header is read with struct."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

_BRAND = Path(__file__).resolve().parent.parent / "brand"
_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_FILES = {"icon.png": (256, 256), "icon@2x.png": (512, 512)}


@pytest.mark.parametrize("name", sorted(_FILES))
def test_brand_png_is_valid(name):
    data = (_BRAND / name).read_bytes()
    assert data.startswith(_SIGNATURE)
    length, kind = struct.unpack(">I4s", data[8:16])
    assert kind == b"IHDR" and length == 13
    assert struct.unpack(">II", data[16:24]) == _FILES[name]
    assert len(data) < 100 * 1024


def test_brand_folder_has_only_the_icons():
    assert {p.name for p in _BRAND.iterdir()} == set(_FILES)


@pytest.mark.parametrize("name", sorted(_FILES))
def test_home_assistant_brand_lookup_serves_them(name):
    from homeassistant.components.brands import _read_brand_file

    assert _read_brand_file(_BRAND, name) == (_BRAND / name).read_bytes()
