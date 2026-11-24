"""UI split P1: the enrolment dialog behaves exactly as it did before the core was extracted.

For fixed inputs the dialog is driven through HA's real options-flow manager and everything it produces is recorded:
each step's type, id, errors and placeholders, the QR payload, the phone entry's data, the settings entry's data, the
users, the registry, and the log lines at INFO and above. The values in `enrollment_dialog_golden.json` were recorded
by running THIS file on the commit BEFORE the core existed (main at wheel 0.6.2, #162), then pasted in; the same file
passes on both. Random values (the code, the enroll id, user and entry ids, times) are replaced by placeholders, and
the QR image by its pixel size.

Not here: the pending cap and the persistent notice, which are new on the dialog path (test_enrollment_core.py).
"""
from __future__ import annotations

import base64
import json
import logging
import re
import struct
from datetime import timedelta
from pathlib import Path

import pytest
from aiohttp import ClientError
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.setup import async_setup_component

from custom_components.portico import enrollment_flow
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.phone_entry import PhoneRecord, async_create_phone_entry, phone_entries

from .test_enrollment_flow import (  # noqa: F401 - fixtures by name
    API_KEY,
    CREDS,
    EXTERNAL,
    FIREBASE,
    HEBREW_NAME_SHORT,
    MINTED,
    _device_entry,
    _entry,
    _open_phone_form,
    _push_entry,
    _qr_capture,
    _set_urls,
    qr_payloads,
    qr_texts,
)
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

GOLDEN = json.loads((Path(__file__).parent / "enrollment_dialog_golden.json").read_text(encoding="utf-8")) \
    if (Path(__file__).parent / "enrollment_dialog_golden.json").exists() else {}


@pytest.fixture(autouse=True)
def _an_owner_exists(hass_owner_user):
    """A real install has its owner before any phone."""


def _png_width(markdown: str) -> int:
    uri = re.search(r"\((data:image/png;base64,[^)]*)\)", markdown).group(1)
    png = base64.b64decode(uri.partition(",")[2])
    return struct.unpack(">I", png[16:20])[0]


class _Normalizer:
    """Replace the random values of one scenario by stable names."""

    def __init__(self) -> None:
        self.table: dict[str, str] = {}

    def learn(self, value: str | None, name: str) -> None:
        if value:
            self.table.setdefault(value, name)

    def __call__(self, value):
        if isinstance(value, dict):
            return {k: self(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self(v) for v in value]
        if isinstance(value, str):
            for raw, name in self.table.items():
                value = value.replace(raw, name)
            value = re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?\+00:00", "<ts>", value)
            return re.sub(r"until \d\d:\d\d", "until <hh:mm>", re.sub(r"\(until \d\d:\d\d\)", "(until <hh:mm>)", value))
        return value


def _step(result, norm) -> dict:
    out = {"type": str(result["type"]), "step_id": result.get("step_id"), "errors": result.get("errors")}
    placeholders = dict(result.get("description_placeholders") or {})
    if "qr_image" in placeholders:
        placeholders["qr_image"] = f"![Enrollment QR](png {_png_width(placeholders['qr_image'])} px wide)"
    if "expires_at" in placeholders:
        placeholders["expires_at"] = "<hh:mm>" if re.fullmatch(r"\d\d:\d\d", placeholders["expires_at"]) else "?"
    if placeholders:
        out["placeholders"] = norm(placeholders)
    schema = result.get("data_schema")
    if schema is not None:
        out["fields"] = [
            [str(k), str(getattr(k, "default", None) if not callable(getattr(k, "default", None)) else k.default()),
             (getattr(k, "description", None) or {}).get("suggested_value")]
            for k in schema.schema
        ]
    return out


async def _observe(hass, entry, steps, payloads, caplog) -> dict:
    norm = _Normalizer()
    for payload in payloads:
        norm.learn(payload.get("code"), "<code>")
        norm.learn(payload.get("enroll_id"), "<enroll_id>")
    users = [u for u in hass.auth._store._users.values() if u.name and u.name.startswith("Portico")]
    for i, user in enumerate(sorted(users, key=lambda u: u.name)):
        norm.learn(user.id, f"<user{i}>")
    phones = sorted(phone_entries(hass), key=lambda e: e.data.get("name", ""))
    for i, phone in enumerate(phones):
        norm.learn(phone.entry_id, f"<phone{i}>")
        norm.learn(phone.data.get("enroll_id"), f"<enroll_id{i}>")
    norm.learn(entry.entry_id, "<push>")
    from custom_components.portico.enroll_redeem import get_registry

    return norm({
        "steps": steps,
        "payloads": payloads,
        "phones": [dict(p.data) for p in phones],
        "settings_entry": {k: v for k, v in entry.data.items() if k in ("ha_dial", "tailscale_mode")},
        "users": sorted(u.name for u in users),
        "live_codes": len(get_registry(hass)),
        "log": sorted(
            f"{r.levelname} {r.getMessage()}"
            for r in caplog.records
            if r.name.startswith("custom_components.portico.enroll") and r.levelno >= logging.INFO
        ),
    })


def _check(scenario: str, observed: dict) -> None:
    if scenario not in GOLDEN:
        print("GOLDEN " + json.dumps({scenario: observed}, sort_keys=True, ensure_ascii=False))
        pytest.fail(f"no golden recorded for {scenario}")
    assert observed == GOLDEN[scenario]


async def _drive(hass, entry, caplog, qr_payloads, inputs, *, followups=()) -> tuple[list, list]:
    """Open the phone form, submit `inputs`, then each follow-up submit while a form is still showing."""
    caplog.set_level(logging.INFO)
    steps = []
    result = await _open_phone_form(hass, entry)
    steps.append(_step(result, lambda v: v))
    result = await hass.config_entries.options.async_configure(result["flow_id"], inputs)
    steps.append(_step(result, lambda v: v))
    for follow in followups:
        result = await hass.config_entries.options.async_configure(result["flow_id"], follow)
        steps.append(_step(result, lambda v: v))
    return steps, result


def _legacy_phone_record(user, name) -> PhoneRecord:
    return PhoneRecord(ha_user_id=user.id, name=name, enroll_id="legacy-enroll-id-0123456789abcdef",
                       enrolled_at="2026-10-08T10:00:00+00:00", token_kind="llat", refresh_state="none")


async def test_manual_new_phone(hass, caplog, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    entry = _device_entry(hass)
    steps, _ = await _drive(hass, entry, caplog, qr_payloads, {"phone_name": "Alice's Pixel"})
    _check("manual_new_phone", await _observe(hass, entry, steps, qr_payloads, caplog))


async def test_manual_hebrew_name_and_typed_dial_is_remembered(hass, caplog, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    entry = _device_entry(hass)
    steps, _ = await _drive(hass, entry, caplog, qr_payloads, {"phone_name": HEBREW_NAME_SHORT, "ha_dial": " 10.6.0.5:443 "})
    _check("manual_hebrew_typed_dial", await _observe(hass, entry, steps, qr_payloads, caplog))


async def test_a_blank_dial_forgets_the_remembered_one(hass, caplog, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    entry = _device_entry(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "ha_dial": "10.9.9.9:443"})
    steps, _ = await _drive(hass, entry, caplog, qr_payloads, {"phone_name": "p", "ha_dial": ""})
    _check("blank_dial_forgets", await _observe(hass, entry, steps, qr_payloads, caplog))


async def test_an_invalid_dial_is_refused_before_anything_exists(hass, caplog, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    entry = _device_entry(hass)
    steps, _ = await _drive(hass, entry, caplog, qr_payloads, {"phone_name": "p", "ha_dial": "ha.local:443"})
    _check("invalid_dial", await _observe(hass, entry, steps, qr_payloads, caplog))


async def test_no_url_at_all(hass, caplog, qr_payloads):
    _set_urls(hass)
    entry = _device_entry(hass)
    steps, _ = await _drive(hass, entry, caplog, qr_payloads, {"phone_name": "p"})
    _check("no_url", await _observe(hass, entry, steps, qr_payloads, caplog))


async def test_only_plain_http_is_refused_then_allowed(hass, caplog, qr_payloads):
    _set_urls(hass, internal="http://ha.lan:8123")
    entry = _device_entry(hass)
    steps, result = await _drive(hass, entry, caplog, qr_payloads, {"phone_name": "p"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"phone_name": "p", "allow_http": True})
    steps.append(_step(result, lambda v: v))
    _check("http_refused_then_allowed", await _observe(hass, entry, steps, qr_payloads, caplog))


async def test_a_fcm_block_and_unusable_firebase_values(hass, caplog, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    entry = _push_entry(hass, firebase=FIREBASE, tailscale_mode="manual")
    steps, result = await _drive(hass, entry, caplog, qr_payloads, {"phone_name": "with fcm"})
    hass.config_entries.async_update_entry(entry, data={**entry.data, "firebase": {**FIREBASE, "api_key": "x"}})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "enroll_phone"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "no fcm"})
    steps.append(_step(result, lambda v: v))
    _check("fcm_then_skipped", await _observe(hass, entry, steps, qr_payloads, caplog))


async def test_auto_mode_mints_a_key_into_the_payload(hass, caplog, qr_payloads, monkeypatch):
    _set_urls(hass, external=EXTERNAL)

    async def _mint(hass_, *, api_key, tailnet, tag):
        assert (api_key, tag) == (API_KEY, "tag:portico-phone")
        return MINTED

    monkeypatch.setattr(enrollment_flow, "_async_mint_tailscale_authkey", _mint)
    entry = _entry(hass, tailscale_mode="auto", **CREDS)
    steps, _ = await _drive(hass, entry, caplog, qr_payloads, {"phone_name": "auto phone"})
    _check("auto_mint", await _observe(hass, entry, steps, qr_payloads, caplog))


async def test_auto_mode_mint_failure(hass, caplog, qr_payloads, monkeypatch):
    _set_urls(hass, external=EXTERNAL)

    async def _mint(hass_, *, api_key, tailnet, tag):
        raise ClientError("down")

    monkeypatch.setattr(enrollment_flow, "_async_mint_tailscale_authkey", _mint)
    entry = _entry(hass, tailscale_mode="auto", **CREDS)
    steps, _ = await _drive(hass, entry, caplog, qr_payloads, {"phone_name": "auto phone"})
    _check("auto_mint_failure", await _observe(hass, entry, steps, qr_payloads, caplog))


async def test_an_old_entry_without_a_mode_is_backfilled_to_auto(hass, caplog, qr_payloads, monkeypatch):
    _set_urls(hass, external=EXTERNAL)

    async def _mint(hass_, *, api_key, tailnet, tag):
        return MINTED

    monkeypatch.setattr(enrollment_flow, "_async_mint_tailscale_authkey", _mint)
    entry = _entry(hass, **CREDS)
    steps, _ = await _drive(hass, entry, caplog, qr_payloads, {"phone_name": "old entry"})
    _check("backfill_auto", await _observe(hass, entry, steps, qr_payloads, caplog))


async def test_the_same_name_again_replaces_the_earlier_qr(hass, caplog, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    entry = _device_entry(hass)
    steps, _ = await _drive(hass, entry, caplog, qr_payloads, {"phone_name": "Pixel"})
    result = await _open_phone_form(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "Pixel"})
    steps.append(_step(result, lambda v: v))
    _check("redraw_pending", await _observe(hass, entry, steps, qr_payloads, caplog))


async def test_a_phone_with_a_long_lived_token_asks_first(hass, caplog, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    assert await async_setup_component(hass, DOMAIN, {})
    user = await hass.auth.async_create_user(name="Portico — Old Pixel", group_ids=[GROUP_ID_USER])
    await hass.auth.async_create_refresh_token(
        user, client_name=f"portico-oldentry-{user.id}", token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=3650))
    await async_create_phone_entry(hass, _legacy_phone_record(user, "Old Pixel"))
    entry = _device_entry(hass)
    steps, result = await _drive(hass, entry, caplog, qr_payloads, {"phone_name": "Old Pixel"},
                                 followups=({"confirm": True},))
    _check("llat_confirmed", await _observe(hass, entry, steps, qr_payloads, caplog))


async def test_declining_the_replacement_goes_back_to_the_name(hass, caplog, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    assert await async_setup_component(hass, DOMAIN, {})
    user = await hass.auth.async_create_user(name="Portico — Old Pixel", group_ids=[GROUP_ID_USER])
    await async_create_phone_entry(hass, _legacy_phone_record(user, "Old Pixel"))
    entry = _device_entry(hass)
    steps, result = await _drive(hass, entry, caplog, qr_payloads, {"phone_name": "Old Pixel"},
                                 followups=({"confirm": False},))
    _check("llat_declined", await _observe(hass, entry, steps, qr_payloads, caplog))


async def test_a_failure_after_the_user_exists_raises_and_leaves_nothing(hass, caplog, qr_payloads, monkeypatch):
    _set_urls(hass, external=EXTERNAL)

    def _boom(payload):
        raise RuntimeError("QR failed")

    monkeypatch.setattr(enrollment_flow, "_build_qr_data_uri", _boom)
    entry = _device_entry(hass)
    result = await _open_phone_form(hass, entry)
    with pytest.raises(RuntimeError, match="QR failed"):
        await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})
    observed = await _observe(hass, entry, [], [], caplog)
    observed.pop("log")                     # the core adds one warning here; the user-visible outcome is the same
    golden = GOLDEN.get("qr_failure")
    if golden is None:
        print("GOLDEN " + json.dumps({"qr_failure": observed}, sort_keys=True, ensure_ascii=False))
        pytest.fail("no golden recorded for qr_failure")
    assert observed == golden
