"""Constants for the push ring notification sender (sender design §2).

`ENDPOINTS` is the ONE place the Google URLs live. Production code reads
`const.ENDPOINTS` at the moment it needs a URL and never assigns it; the tests
replace it (monkeypatch) with a local aiohttp server that stands in for both
Google endpoints, so no test can reach Google. The service-account
`token_uri` is pinned to `ENDPOINTS.token_uri` (§2.3), so in production a
crafted JSON cannot point the signed assertion anywhere else.
"""
from __future__ import annotations

from dataclasses import dataclass

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
FCM_BASE_URL = "https://fcm.googleapis.com"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


@dataclass(frozen=True, slots=True)
class Endpoints:
    token_uri: str
    fcm_base_url: str


DEFAULT_ENDPOINTS = Endpoints(token_uri=GOOGLE_TOKEN_URI, fcm_base_url=FCM_BASE_URL)
# Tests only may replace this. Read it at use time, never import the value.
ENDPOINTS = DEFAULT_ENDPOINTS

# portico-app's package (§2.4 restricted_package_name, §5.2). The push entry
# stores the package it was set up for, defaulting to this.
PORTICO_PACKAGE = "com.meda.portico"

# The push config entry: one per HA instance, beside the device entries.
ENTRY_TYPE = "entry_type"
ENTRY_TYPE_PUSH = "push"
PUSH_UNIQUE_ID = "push"
DATA_PUSH = "push"  # hass.data[DOMAIN][DATA_PUSH] -> PushManager
# The dispatcher signal the manager sends when a registration or the push ring notification's state changes (P-4: the per-phone
# sensors follow it).
SIGNAL_PHONES_CHANGED = "portico_phones_changed"

# Options on the push entry.
CONF_ENABLED = "enabled"
CONF_RING_WINDOW = "ring_window"
DEFAULT_RING_WINDOW = 45  # gate-sip's GATE_SIP_RING_SECONDS default (§4.2)
MIN_RING_WINDOW = 10
MAX_RING_WINDOW = 120

# "New phones get the push ring notification" (P2 plan gap G2, decision Q3, 2026-10-06):
# whether a phone's FIRST registration starts switched on. An option on the
# push entry; a row that exists keeps its own switch whatever this says.
# - A fresh install defaults ON (the public default, sender design §3.4); the
#   config flow always stores the value it was given.
# - An entry created before this option has no stored value: missing means
#   OFF, the behaviour P1 shipped with (sender decision 4), so upgrading never
#   switches on a phone the owner did not.
CONF_NEW_PHONES_ENABLED = "new_phones_enabled"
DEFAULT_NEW_PHONES_ENABLED = True
LEGACY_NEW_PHONES_ENABLED = False


def new_phones_enabled(options) -> bool:
    """The option as stored on the push entry, OFF when the entry predates it."""
    return bool(options.get(CONF_NEW_PHONES_ENABLED, LEGACY_NEW_PHONES_ENABLED))

CREDENTIALS_STORE_KEY = "portico.push_credentials"
REGISTRY_STORE_KEY = "portico.push_registry"

# Repair issue ids (§7).
ISSUE_CREDENTIALS_INVALID = "push_credentials_invalid"
ISSUE_PROJECT_MISMATCH = "push_project_mismatch"
ISSUE_PAYLOAD_REJECTED = "push_payload_rejected"
ISSUE_RATE_LIMITED = "push_rate_limited"

# The staged real-device test (spec §8.3, P2 plan G6): reg_ids of the phones
# `portico.push_test` may send to. Empty by default (and when missing). While
# it is set, a REAL ring goes only to listed phones that are also switched on,
# so a test cannot ring the household.
CONF_TEST_PHONES = "test_phones"
SERVICE_PUSH_TEST = "push_test"
