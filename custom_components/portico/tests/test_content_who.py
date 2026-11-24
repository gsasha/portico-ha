"""Two enrolled phones can have the SAME name (every enrolment makes a new phone user, so a re-enrolled
phone is listed twice). The panel says which is which: the day it was enrolled
and whether its app has registered with the push ring notification."""
from __future__ import annotations

import types

import pytest

from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.content import phone_facts, phone_label

from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name
from .test_signaling import _add_device, _setup_signaling

pytestmark = pytest.mark.asyncio


def _with_push(hass, registered_user_ids):
    """A stand-in push ring notification manager whose registry knows these users."""
    rows = {uid: [{"reg_id": "r"}] for uid in registered_user_ids}
    manager = types.SimpleNamespace(registry=types.SimpleNamespace(rows_for_user=lambda uid: rows.get(uid, [])))
    hass.data.setdefault(DOMAIN, {})["push"] = manager


async def test_labels_say_when_it_was_enrolled_and_whether_the_app_registered(hass):
    _with_push(hass, ["u1"])
    registered = phone_facts(hass, "u1", {"enrolled_at": "2026-10-08T16:20:00+00:00"})
    never = phone_facts(hass, "u2", {"enrolled_at": "2026-10-06T09:00:00+00:00"})
    assert registered == {"enrolled_at": "2026-10-08", "registered": True, "app_version": None}
    assert never == {"enrolled_at": "2026-10-06", "registered": False, "app_version": None}
    assert phone_label("Alice's phone", registered) == "Alice's phone (enrolled 2026-10-08, app registered)"
    assert phone_label("Alice's phone", never) == "Alice's phone (enrolled 2026-10-06, app not registered)"


async def test_without_the_away_ring_it_does_not_guess(hass):
    facts = phone_facts(hass, "u1", {"enrolled_at": "2026-10-08T16:20:00+00:00"})
    assert facts["registered"] is None
    assert phone_label("P", facts) == "P (enrolled 2026-10-08)"
    assert phone_label("P", {"enrolled_at": None, "registered": None}) == "P"


# --- DEV-666: the app version in the label is the LAST SEEN one -------------------------------------------------------


def _with_rows(hass, rows_by_user):
    manager = types.SimpleNamespace(registry=types.SimpleNamespace(rows_for_user=lambda uid: rows_by_user.get(uid, [])))
    hass.data.setdefault(DOMAIN, {})["push"] = manager


async def test_the_label_shows_the_version_the_app_registered_with_last(hass):
    _with_rows(hass, {"u1": [
        {"app_version": "0.4.3-alpha", "last_seen_at": "2026-10-01T10:00:00+00:00"},
        {"app_version": "0.5.10", "last_seen_at": "2026-10-20T10:00:00+00:00"},
        {"app_version": "0.5.2", "last_seen_at": "2026-10-10T10:00:00+00:00"},
    ]})
    facts = phone_facts(hass, "u1", {"enrolled_at": "2026-10-08T16:20:00+00:00"})
    assert facts["app_version"] == "0.5.10"
    assert phone_label("Alice's phone", facts) == "Alice's phone (enrolled 2026-10-08, app 0.5.10)"


async def test_a_registration_with_no_version_says_registered_and_no_registration_says_not(hass):
    _with_rows(hass, {"u1": [{"app_version": "", "last_seen_at": "2026-10-20T10:00:00+00:00"}]})
    blank = phone_facts(hass, "u1", {"enrolled_at": "2026-10-08"})
    never = phone_facts(hass, "u2", {"enrolled_at": "2026-10-08"})
    assert phone_label("P", blank) == "P (enrolled 2026-10-08, app registered)"
    assert never["app_version"] is None and phone_label("P", never) == "P (enrolled 2026-10-08, app not registered)"


async def test_a_new_registration_changes_the_label_with_the_real_registry(hass, hass_storage):
    from custom_components.portico.push import const
    from custom_components.portico.push.registry import PushRegistry

    registry = PushRegistry(hass)
    await registry.async_load()
    kw = dict(ha_user_id="u1", installation_id="i1", kind="fcm", registration="r" * 20, package=const.PORTICO_PACKAGE,
              fcm_project_id="p", name="A")
    await registry.async_register(app_version="0.4.3", **kw)
    hass.data.setdefault(DOMAIN, {})["push"] = types.SimpleNamespace(registry=registry)
    assert phone_facts(hass, "u1", {})["app_version"] == "0.4.3"
    await registry.async_register(app_version="0.5.10", **kw)
    assert phone_label("P", phone_facts(hass, "u1", {})) == "P (app 0.5.10)"
