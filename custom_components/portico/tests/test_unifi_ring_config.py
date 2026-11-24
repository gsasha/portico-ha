"""unifi_protect's `ring_events` option: optional, boolean, off when left out,
labelled in the form, and handed to the transport untouched. No HA harness."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import voluptuous as vol

from custom_components.portico.door.drivers import unifi_protect

_BASE = {"nvr_host": "nvr.example", "nvr_api_key": "KEY", "camera_id": "c"}
_ROOT = Path(__file__).resolve().parent.parent


def test_ring_events_is_optional_and_absent_by_default():
    out = unifi_protect.CONFIG_SCHEMA(dict(_BASE))
    assert "ring_events" not in out  # no default: the transport sees nothing and stays off


@pytest.mark.parametrize("value", [True, False])
def test_ring_events_accepts_a_bool(value):
    assert unifi_protect.CONFIG_SCHEMA({**_BASE, "ring_events": value})["ring_events"] is value


def test_ring_events_rejects_a_non_bool():
    with pytest.raises(vol.Invalid):
        unifi_protect.CONFIG_SCHEMA({**_BASE, "ring_events": "yes please"})


def test_ring_events_has_form_text_in_both_files():
    for rel in ("strings.json", "translations/en.json"):
        data = json.loads((_ROOT / rel).read_text(encoding="utf-8"))
        assert data["config"]["step"]["driver_config"]["data"]["ring_events"]
