"""`portico/phones/make_panel` (own pages): the make-a-panel command without a slug, the deprecated alias, and that a legacy
`panel_screen` option is ignored."""
from __future__ import annotations

import pytest

from .. import phone_entry
from ..phones_commands import async_setup_phones_commands
from .test_panel_own_pages import FORMER, PANEL, _ids, _store, pan  # noqa: F401 - `pan` is a fixture, by name
from .test_pages_pc import _send, pworld  # noqa: F401
from .test_signaling import _setup_signaling  # noqa: F401
from .test_push_registry import _phone_user

pytestmark = pytest.mark.asyncio


async def _admin(world, hass_ws_client):
    async_setup_phones_commands(world.hass)
    await _setup_signaling(world.hass)
    return await hass_ws_client(world.hass)


async def test_make_panel_has_no_slug_declares_no_screen_and_the_alias_ignores_the_slug(pan, hass_ws_client):
    store = await _store(pan, {})
    # a plain Portico-made phone becomes a panel through the new command
    user, _ = await _phone_user(pan.hass, name="Portico — Fresh panel")
    fresh = phone_entry_for(pan, user, "ep-fresh", "Fresh panel")
    admin = await _admin(pan, hass_ws_client)
    before = dict(store.dashboards)
    got = await _send(admin, "portico/phones/make_panel", 1, entry_id=fresh.entry_id)
    assert got["success"] and got["result"]["result"] == "updated" and got["result"]["phone"]["panel"] is True, got
    assert "panel_screen" not in got["result"]["phone"] and "panel_screen" not in fresh.options
    assert store.dashboards == before                                              # no screen declared
    assert (await _send(admin, "portico/phones/make_panel", 2, entry_id=fresh.entry_id))["error"]["code"] == "already_panel"
    assert (await _send(admin, "portico/phones/make_panel", 3, entry_id="no-such"))["error"]["code"] == "unknown_phone"
    # the old command name still works for one release and ignores `slug` (even a bad one)
    user2, _ = await _phone_user(pan.hass, name="Portico — Alias panel")
    alias = phone_entry_for(pan, user2, "ep-alias", "Alias panel")
    got = await _send(admin, "portico/phones/make_panel_screen", 4, entry_id=alias.entry_id, slug="Not A Slug")
    assert got["success"] and got["result"]["phone"]["panel"] is True and store.dashboards == before
    assert (await _send(admin, "portico/phones/make_panel_screen", 5, entry_id=alias.entry_id))["error"]["code"] == "already_panel"   # slug optional


async def test_the_phones_list_row_of_a_panel_never_carries_the_legacy_screen(pan, hass_ws_client):
    await _store(pan, {FORMER: ["p-gate"]})
    admin = await _admin(pan, hass_ws_client)
    assert pan.entry.options.get("panel_screen") == FORMER                         # the legacy option is still on the entry
    rows = (await _send(admin, "portico/phones/list", 1))["result"]["phones"]
    row = next(r for r in rows if r["entry_id"] == pan.entry.entry_id)
    assert row["panel"] is True and "panel_screen" not in row


def phone_entry_for(world, user, enroll_id, name):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from ..config_flow import DOMAIN
    from ..phone_entry import PhoneRecord

    record = PhoneRecord(ha_user_id=user.id, name=name, enroll_id=enroll_id, enrolled_at="2026-11-19T10:00:00+00:00")
    entry = MockConfigEntry(domain=DOMAIN, title=name, unique_id=f"phone:{record.key}", data=record.to_data(), options={"house_receive": True})
    entry.add_to_hass(world.hass)
    return entry
