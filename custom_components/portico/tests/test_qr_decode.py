"""UI split P2: does the QR the panel shows READ, with nothing in the way patched?

Design section 8 names the equivalents of a feature that passes its tests and fails on the device: "a QR nobody decoded".
So these tests build the longest realistic enrolment (a 24-character name, the limit, an auto-minted Tailscale key, the Firebase
block, an `ha_dial`) through the REAL path (websocket command, enrolment core, the real `_make_qr`, the real view), take
the PNG the browser would fetch, and:

  * always (CI): check the PNG is a QR at one pixel per module with the 4-module quiet zone, its module count, and that
    an integer upscale (the panel's `k` of 3 to 6) keeps every module on a pixel boundary;
  * when a REAL decoder is configured: decode the PNG upscaled to k = 3, 4, 5 and 6 (the panel never draws below 3: one pixel per module is not readable, by ZXing or a phone) and compare the text with what the
    dialog's own serialiser (`enrollment_flow._qr_text`) makes of the decoded fields, check the fields are this
    enrolment's, and REDEEM the code in the QR, once.

The decoder is not in the test venv (no zxing-cpp, OpenCV or pyzbar wheel for Python 3.14 is installed, and the venvs run
with no network). `PORTICO_QR_DECODER` is a command that takes a PNG path and prints the text it holds on stdout and
exits non-zero when it cannot read it; `scripts/qr_decode_check.sh` runs these tests with ZXing core 3.5.3 (the version
portico-app's `QrFrameDecoder` uses) in a JDK. Without the variable the decoding test is skipped, loudly.
"""
from __future__ import annotations

import io
import json
import os
import shlex
import subprocess

import pytest
from homeassistant.setup import async_setup_component

from custom_components.portico import enrollment_flow
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.enroll_view import QR_PATH
from custom_components.portico.phone_entry import phone_entries

from .redeem_helpers import redeem
from .test_enrollment_flow import CREDS, EXTERNAL, FIREBASE, _push_entry, _set_urls
from .test_phones_commands import Ws, enrol_msg, ok
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio

DECODER_ENV = "PORTICO_QR_DECODER"
DUMP_ENV = "PORTICO_QR_DUMP_DIR"           # optional: also leave the PNGs here, for a look
LONG_KEY = "tskey-auth-k" + "k" * 11 + "CNTRL-" + "t" * 40                    # the owner's real key shape (69 characters)
NAME_ASCII = "Alice's Pixel 10 Pro, ok"                                          # exactly 24 characters
NAME_HEBREW = "ט" * 24                                                          # 24 characters, 48 bytes: the densest case (version 18)
HA_DIAL = "10.6.0.5:443"
OWNER_URL = "https://haos.myhouse-01.example.org"

assert len(NAME_ASCII) == 24 and len(NAME_HEBREW) == 24


@pytest.fixture(autouse=True)
def _an_owner_exists(hass_owner_user):
    """A real install has its owner before any phone."""


@pytest.fixture
async def world(hass, hass_ws_client, hass_client, monkeypatch):
    assert await async_setup_component(hass, DOMAIN, {})
    _set_urls(hass, external=OWNER_URL)
    _push_entry(hass, firebase=FIREBASE, tailscale_mode="auto", **CREDS)

    async def _mint(hass_, *, api_key, tailnet, tag):
        return LONG_KEY

    # The key is minted by Tailscale in real life; the QR builder and everything after it are NOT patched.
    monkeypatch.setattr(enrollment_flow, "_async_mint_tailscale_authkey", _mint)
    return {"ws": Ws(await hass_ws_client(hass)), "admin": await hass_client()}


async def fetch_png(world, name):
    created = ok(await world["ws"].send(enrol_msg(name, ha_dial=HA_DIAL)))
    resp = await world["admin"].get(f"{QR_PATH}/{created['qr_token']}")
    assert resp.status == 200 and resp.content_type == "image/png"
    return created, await resp.read()


def matrix(png: bytes, modules: int) -> list[list[bool]]:
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("L")
    assert image.size == (modules + 8, modules + 8)
    return [[image.getpixel((4 + x, 4 + y)) < 128 for x in range(modules)] for y in range(modules)]


@pytest.mark.parametrize("name", [NAME_ASCII, NAME_HEBREW])
async def test_the_longest_realistic_enrolment_makes_a_qr_the_panel_can_scale(hass, world, name):
    from PIL import Image

    created, png = await fetch_png(world, name)
    modules = created["modules"]
    assert (modules - 17) % 4 == 0 and modules in ((85,) if name == NAME_ASCII else (85, 89)), modules   # 24 ASCII characters stay version 17
    assert (modules - 17) % 4 == 0
    grid = matrix(png, modules)
    # finder patterns at three corners, timing pattern on row 6: it is a QR
    for ox, oy in ((0, 0), (modules - 7, 0), (0, modules - 7)):
        assert all(grid[oy][ox + i] and grid[oy + 6][ox + i] and grid[oy + i][ox] and grid[oy + i][ox + 6] for i in range(7))
    assert [grid[6][i] for i in range(8, modules - 8)] == [i % 2 == 0 for i in range(8, modules - 8)]
    # every integer upscale the panel may choose (k = 3..6) keeps each module a k x k block of one colour
    base = Image.open(io.BytesIO(png)).convert("L")
    for k in (3, 4, 5, 6):
        big = base.resize((base.width * k, base.height * k), Image.NEAREST)
        for y in range(0, modules, 7):
            for x in range(0, modules, 5):
                px = [big.getpixel(((4 + x) * k + dx, (4 + y) * k + dy)) for dx in range(k) for dy in range(k)]
                assert len(set(px)) == 1 and (px[0] < 128) == grid[y][x]
    phone = phone_entries(hass)[-1]
    assert phone.data["name"] == name


SCALES = (3, 4, 5, 6)          # the panel never draws below k = 3: one pixel per module is not readable, by ZXing or a phone
ROUNDS = 5                      # enrolments per case: a QR's code and enroll id are random and ZXing's detector is data-dependent


def _name_of(length: int, i: int) -> str:
    import random

    rng = random.Random(1000 + i)
    stem = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz '-") for _ in range(length - 2)).strip() or "x"
    return (f"{i}" + stem + "z" * length)[:length]


def _decode(command: str, path) -> str | None:
    run = subprocess.run(shlex.split(command) + [str(path)], capture_output=True, timeout=120)
    return run.stdout.decode("utf-8") if run.returncode == 0 else None


# (case id, name length, must every QR read?). 12 characters is the owner's real payload (477 bytes, version 17, 85 modules); 24 is
# the limit of a phone name (Alice, 2026-10-23): 489 bytes of text for a 24-character ASCII name, still version 17. At 40
# characters (505 bytes, version 18, 89 modules) ZXing 3.5.3 did not find most codes at any scale, which is why the limit is 24.
@pytest.mark.parametrize("case, length, must", [("realistic-12", 12, True), ("limit-24", 24, True)])
async def test_a_real_decoder_reads_the_qr_at_every_scale_and_the_code_in_it_redeems(
        hass, world, tmp_path, case, length, must):
    command = os.environ.get(DECODER_ENV)
    if not command:
        pytest.skip(f"{DECODER_ENV} is not set: no real QR decoder in this environment (run scripts/qr_decode_check.sh)")
    from PIL import Image

    unread: list[str] = []
    sizes: set[int] = set()
    for i in range(ROUNDS):
        name = _name_of(length, i)
        created, png = await fetch_png(world, name)
        base = Image.open(io.BytesIO(png)).convert("RGB")
        texts: dict[int, str | None] = {}
        for k in SCALES:
            path = tmp_path / f"qr-{case}-{i}-k{k}.png"
            image = base.resize((base.width * k, base.height * k), Image.NEAREST)
            image.save(path)
            dump = os.environ.get(DUMP_ENV)
            if dump:
                os.makedirs(dump, exist_ok=True)
                image.save(os.path.join(dump, path.name))
            texts[k] = await hass.async_add_executor_job(_decode, command, path)
        unread += [f"round {i} v{(created['modules'] - 17) // 4} k{k}" for k, t in texts.items() if t is None]
        got = {t for t in texts.values() if t is not None}
        assert len(got) <= 1, "the decoder read different text at different scales"
        if got:
            text = got.pop()
            sizes.add(len(text.encode("utf-8")))
            payload = json.loads(text)
            # byte for byte what the dialog's serialiser makes of the same fields
            assert enrollment_flow._qr_text(payload) == text
            phone = next(p for p in phone_entries(hass) if p.data["name"] == name)
            assert payload["portico"] == 2 and payload["phone_name"] == name and payload["ha_url"] == OWNER_URL
            assert payload["enroll_id"] == phone.data["enroll_id"] and payload["tailscale_authkey"] == LONG_KEY
            assert payload["ha_dial"] == HA_DIAL and payload["fcm"] == {"v": 1, **FIREBASE}
            assert set(payload) == {"portico", "ha_url", "code", "phone_name", "enroll_id", "tailscale_authkey", "ha_dial", "fcm"}
            # and the code the phone reads from it is live: it redeems, once
            body = await redeem(hass, payload)
            assert body["access_token"] and body["refresh_token"]
        else:
            ok(await world["ws"].send({"type": "portico/phones/cancel", "entry_id": created["entry_id"]}))   # free the pending slot
    total = ROUNDS * len(SCALES)
    print(f"{case}: {total - len(unread)}/{total} reads (ZXing default, scales {SCALES}); payload bytes {sorted(sizes)}")
    if unread and must:
        pytest.fail(f"{case}: ZXing could not read {len(unread)} of {total}: {unread}")
    if unread:
        pytest.xfail(f"{case}: ZXing 3.5.3 read {total - len(unread)} of {total} (version 18, 89 modules): {unread[:6]}...")
