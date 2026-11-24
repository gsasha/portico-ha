"""Tests for the Lovelace card's registration (§9.7): a real static
path served for real over real HTTP, and frontend.add_extra_js_url's
real data-registration behavior — both verified directly, not assumed
from the API's shape.

Split from a single "call async_setup and check everything" test on
purpose: HA's real `frontend` component setup needs the separately
versioned, large compiled-UI `hass-frontend` package, which isn't
available in this sandboxed environment (confirmed — pip has no
`hass-frontend` distribution reachable here). That's an environment
constraint, not a reason to skip verifying this module's own logic:
`hass.http.async_register_static_paths` only needs the `http` component
(no `hass-frontend` dependency), and `frontend.add_extra_js_url`'s own
source (read directly, not assumed) is a one-line `hass.data[key].add(url)`
— seeding that same data key here, exactly like the real frontend
component's own setup does, tests the real function for real without
needing the full component.
"""
from __future__ import annotations

import pytest
from homeassistant.setup import async_setup_component

pytestmark = pytest.mark.asyncio


async def test_static_path_serves_the_real_card_file_over_real_http(hass, hass_client):
    import portico

    assert await async_setup_component(hass, "http", {})
    await hass.async_block_till_done()

    import os

    card_path = os.path.join(os.path.dirname(portico.__file__), "www", "portico-card.js")
    portico._async_serve_script(hass, portico._CARD_URL_PATH, card_path)

    client = await hass_client()
    resp = await client.get(portico._CARD_URL_PATH)
    assert resp.status == 200
    body = await resp.text()
    assert 'customElements.define("portico-card"' in body


async def test_add_extra_js_url_registers_the_real_card_url(hass):
    from homeassistant.components import frontend
    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

    import portico

    # Matches what the real frontend component's own async_setup
    # initializes before any integration calls add_extra_js_url — seeded
    # directly here since the full component isn't available in this
    # sandbox (see module docstring).
    hass.data[DATA_EXTRA_MODULE_URL] = set()

    frontend.add_extra_js_url(hass, portico._CARD_URL_PATH)

    assert portico._CARD_URL_PATH in hass.data[DATA_EXTRA_MODULE_URL]


# ---- The registered URLs carry a content hash of the served file --------------------------------------------------
# The manifest version did not move when the card JS changed, and the kiosk kept the old card: the `?v=` token is the
# first 12 hex chars of the sha256 of each file's bytes, computed once at setup, off the event loop.


def _www(name: str) -> str:
    import os

    import portico

    return os.path.join(os.path.dirname(portico.__file__), "www", name)


def _sha12(path: str) -> str:
    import hashlib

    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()[:12]


def test_the_token_is_12_hex_chars_of_the_sha256(tmp_path):
    import re

    import portico

    f = tmp_path / "x.js"
    f.write_bytes(b"console.log(1);\n")
    token = portico._content_token(str(f))
    assert re.fullmatch(r"[0-9a-f]{12}", token)
    assert token == _sha12(str(f))


def test_the_token_is_stable_for_the_same_bytes_and_changes_with_one_byte(tmp_path):
    import portico

    a, b, c = tmp_path / "a.js", tmp_path / "b.js", tmp_path / "c.js"
    a.write_bytes(b"customElements.define('x', y);")
    b.write_bytes(b"customElements.define('x', y);")      # same bytes, another file
    c.write_bytes(b"customElements.define('x', z);")      # one byte different
    assert portico._content_token(str(a)) == portico._content_token(str(b))
    assert portico._content_token(str(a)) != portico._content_token(str(c))


def test_an_unreadable_file_falls_back_to_the_manifest_version_with_a_warning(tmp_path, caplog, monkeypatch):
    import portico

    monkeypatch.setattr(portico, "_VERSION", "9.9.9")
    with caplog.at_level("WARNING"):
        token = portico._content_token(str(tmp_path / "missing.js"))
    assert token == "9.9.9"
    assert "missing.js" in caplog.text and "manifest version" in caplog.text


def test_versioned_appends_the_token_and_an_empty_token_leaves_the_bare_url():
    import portico

    assert portico._versioned(portico._CARD_URL_PATH, "abc123abc123") == "/portico/portico-card.js?v=abc123abc123"
    assert portico._versioned(portico._PANEL_URL_PATH, "") == "/portico/portico-panel.js"


async def test_the_versioned_url_is_served_by_the_bare_static_path(hass, hass_client):
    """The query string is only a cache key: the static path registered is the bare one and the file comes back."""
    import portico

    assert await async_setup_component(hass, "http", {})
    await hass.async_block_till_done()

    card_path = _www("portico-card.js")
    portico._async_serve_script(hass, portico._CARD_URL_PATH, card_path)
    resp = await (await hass_client()).get(portico._versioned(portico._CARD_URL_PATH, portico._content_token(card_path)))
    assert resp.status == 200
    assert 'customElements.define("portico-card"' in await resp.text()


async def test_a_stale_token_redirects_to_the_current_one_and_is_never_cached(hass, hass_client):
    """DEV-768: the registered URLs are fixed at startup, so after the files change a client still asks for `?v=<old>`.
    That must not be answered with the file under the old name: it redirects (no-store) to the URL of the present bytes."""
    import portico

    assert await async_setup_component(hass, "http", {})
    await hass.async_block_till_done()

    card_path = _www("portico-card.js")
    portico._async_serve_script(hass, portico._CARD_URL_PATH, card_path)
    client = await hass_client()
    current = portico._versioned(portico._CARD_URL_PATH, portico._content_token(card_path))

    stale = await client.get(portico._CARD_URL_PATH + "?v=000000000000", allow_redirects=False)
    assert stale.status == 302
    assert stale.headers["Location"] == current
    assert stale.headers["Cache-Control"] == "no-store"

    followed = await client.get(portico._CARD_URL_PATH + "?v=000000000000")      # a browser follows it
    assert followed.status == 200 and 'customElements.define("portico-card"' in await followed.text()
    assert (await client.get(current, allow_redirects=False)).status == 200
    assert (await client.get(portico._CARD_URL_PATH, allow_redirects=False)).status == 200
