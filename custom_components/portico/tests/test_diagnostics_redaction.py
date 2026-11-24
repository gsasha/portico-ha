"""DEV-700 option A: the diagnostics download redacts by a RULE, and a test fails when a new secret is not covered.

Three things are proved here:

* the rule itself (key patterns, the allow-list, pseudonyms, the URL-credentials value rule, `open_relay_action`, the
  input is never changed);
* a realistic fake entry of each kind (push, phone, and the three door drivers) comes out of the real
  `async_get_config_entry_diagnostics` with no planted secret anywhere in the serialised text;
* the GUARD: every field name a config-flow schema asks for (collected from the flow source and the driver schemas,
  not from a list kept here) that looks like a secret is either redacted or on the allow-list AND acknowledged below.
  Adding `vol.Optional("webhook_secret")` to a flow without thinking about diagnostics fails this file.

Plain HA-harness session. Every secret below is an obviously fake `SENTINEL-` string.
"""
from __future__ import annotations

import ast
import copy
import importlib
import json
import re
import sys
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico import diagnostics as diag_mod
from custom_components.portico.diagnostics import (
    ALLOWED_KEYS,
    REDACTED,
    async_get_config_entry_diagnostics,
    redact_diagnostics,
)

PKG = Path(diag_mod.__file__).parent

# ---- fake entries, one per kind, every field the flows can write ---------------------------------------------------

S = "SENTINEL-SECRET-123"
HOST = "SENTINEL-HOST-10.99.0.7"
USER = "SENTINEL-USER-9901#0"

ACTION = [
    {"action": "lock.open", "target": {"entity_id": "lock.front_gate"}, "data": {"code": S, "note": "x"}},
    {"action": "rest_command.open", "data": {"headers": {"Authorization": f"Bearer {S}"}}},
    {"action": "notify.mobile", "data_template": {"message": "{{ '" + S + "' }}"}},
]

DAHUA = {
    "entry_type": "door", "driver": "dahua_sip", "host": HOST, "sip_user": USER, "sip_password": S,
    "rtsp_username": f"{USER}-rtsp", "rtsp_password": S, "existing_camera_entity": "camera.front_door",
    "open_relay_action": ACTION,
}
UNIFI = {
    "entry_type": "door", "driver": "unifi_protect", "nvr_host": HOST, "nvr_api_key": S,
    "camera_id": f"SENTINEL-CAMERA-{S}", "verify_ssl": False, "ring_events": True,
    "existing_camera_entity": None, "open_relay_action": [],
}
OBSERVER = {
    "entry_type": "door", "driver": "gate_sip_observer", "gate_sip_url": f"http://{HOST}:8080", "interval": 5.0,
    "existing_camera_entity": "camera.gate",
    "gate_sip_listen": {"enabled": True, "audio_port": 8092, "claims": True, "dial": True,
                        "ring_answered_entity": "binary_sensor.gate_answered"},
    "open_relay_action": [{"action": "lock.open", "target": {"entity_id": "lock.gate"}}],
}
PUSH = {
    "entry_type": "push", "project_id": "portico-prod", "client_email": f"SENTINEL-sa@{HOST}.iam.example",
    "private_key_id": S, "package": "il.home.portico",
    "firebase": {"project_id": "portico-prod", "sender_id": "1234", "app_id": "1:1234:android:abc", "api_key": S},
    "tailscale_mode": "auto", "tailscale_api_key": f"tskey-client-{S}", "tailscale_tailnet": f"SENTINEL-{HOST}.ts.net",
    "tailscale_tag": "tag:portico-phone", "tailscale_client_id": f"SENTINEL-CLIENT-{S}",
    "tailscale_redirect_uri": f"https://{HOST}/auth", "ha_dial": f"https://{HOST}:8123",
    "enrolled_phones": [{"ha_user_id": f"SENTINEL-UID-{S}", "name": f"SENTINEL-NAME-{S}", "enroll_id": S}],
}
PHONE = {
    "entry_type": "phone", "ha_user_id": f"SENTINEL-UID-{S}", "name": f"SENTINEL-NAME-{S}", "enroll_id": S,
    "enrolled_at": "2026-10-24T08:00:00+03:00", "token_kind": "refresh", "token_expires_at": "2026-10-25T08:00:00",
    "refresh_state": "active", "tailscale_mode": "auto", "tailscale_node": f"SENTINEL-NODE-{S}", "state": "enrolled",
    "pending_since": None,
}
PUSH_OPTIONS = {"enabled": True, "ring_window": 30, "new_phones_enabled": True, "test_phones": ["a"], "peer_token": S}

TITLES = {"dahua": "Portico (dahua_sip)", "unifi": "Portico (unifi_protect)", "observer": "Portico (gate_sip_observer)",
          "push": "Portico push ring notification", "phone": f"SENTINEL-NAME-{S}"}
KINDS = {"dahua": DAHUA, "unifi": UNIFI, "observer": OBSERVER, "push": PUSH, "phone": PHONE}


def _entry(hass, kind: str) -> MockConfigEntry:
    options = PUSH_OPTIONS if kind == "push" else {"away_media": True, "doors": ["x"]} if kind == "phone" else {}
    entry = MockConfigEntry(domain="portico", unique_id=f"sentinel-{kind}", title=TITLES[kind],
                            data=copy.deepcopy(KINDS[kind]), options=copy.deepcopy(options))
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_no_planted_secret_appears_anywhere_in_a_kinds_download(hass, kind):
    entry = _entry(hass, kind)
    before = (copy.deepcopy(dict(entry.data)), copy.deepcopy(dict(entry.options)))
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diag, default=str)
    assert "SENTINEL" not in text and "tskey-" not in text
    assert (dict(entry.data), dict(entry.options)) == before          # the entry itself is never changed
    assert diag["entry"]["data"]["entry_type"] == KINDS[kind]["entry_type"]


async def test_what_support_needs_stays_readable(hass):
    dahua = (await async_get_config_entry_diagnostics(hass, _entry(hass, "dahua")))["entry"]["data"]
    assert dahua["driver"] == "dahua_sip" and dahua["existing_camera_entity"] == "camera.front_door"
    assert dahua["sip_password"] == dahua["rtsp_password"] == dahua["sip_user"] == dahua["rtsp_username"] == REDACTED
    assert dahua["host"].startswith("redacted-")
    # the relay action keeps WHAT it runs and on WHAT, and nothing it could carry a secret in
    assert dahua["open_relay_action"][0] == {
        "action": "lock.open", "target": {"entity_id": "lock.front_gate"}, "data": REDACTED}
    assert dahua["open_relay_action"][1] == {"action": "rest_command.open", "data": REDACTED}
    assert dahua["open_relay_action"][2]["data_template"] == REDACTED

    observer = (await async_get_config_entry_diagnostics(hass, _entry(hass, "observer")))["entry"]["data"]
    assert observer["interval"] == 5.0 and observer["driver"] == "gate_sip_observer"
    assert observer["gate_sip_listen"] == OBSERVER["gate_sip_listen"]
    assert observer["gate_sip_url"].startswith("redacted-")

    push = await async_get_config_entry_diagnostics(hass, _entry(hass, "push"))
    assert push["entry"]["data"]["tailscale_tag"] == "tag:portico-phone"
    assert push["entry"]["data"]["firebase"]["project_id"] == "portico-prod"
    assert push["entry"]["data"]["firebase"]["api_key"] == REDACTED
    assert push["entry"]["data"]["tailscale_api_key"] == REDACTED
    assert push["entry"]["options"]["ring_window"] == 30 and push["entry"]["options"]["peer_token"] == REDACTED

    phone = await async_get_config_entry_diagnostics(hass, _entry(hass, "phone"))
    data = phone["entry"]["data"]
    assert (data["token_kind"], data["refresh_state"], data["state"]) == ("refresh", "active", "enrolled")
    assert data["token_expires_at"] == "2026-10-25T08:00:00" and data["enroll_id"] == REDACTED
    assert phone["entry"]["title"] == "Portico phone"


def test_a_pseudonym_is_stable_within_one_download_and_not_between_two():
    data = {"a": {"host": HOST}, "b": [{"nvr_host": HOST}, {"nvr_host": "other-host"}]}
    one = redact_diagnostics(data)
    assert one["a"]["host"] == one["b"][0]["nvr_host"] != one["b"][1]["nvr_host"]
    assert one["a"]["host"] != redact_diagnostics(data)["a"]["host"]      # salted per download: no brute-forcing a host


# ---- the rule --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", [
    "sip_password", "SIP_PASSWORD", "wifi_passwd", "webhook_secret", "fcm_secret", "refresh_token", "authkey",
    "x_api_key", "apikey", "Credentials", "oauth_client", "private_blob", "webhook_token", "rtsp_anything", "sip_user",
    "username", "enroll_id", "registration", "session_cookie",
])
def test_a_secret_looking_key_is_redacted_at_any_depth(key):
    out = redact_diagnostics({"entry": {"data": {"deep": [{"deeper": {key: S}}]}}})
    assert out["entry"]["data"]["deep"][0]["deeper"][key] == REDACTED


@pytest.mark.parametrize("value", [
    "rtsp://admin:SENTINEL-pw@10.1.2.3:554/cam/1", "http://SENTINEL-user@host/x", "https://u:p@h",
    "http://h/path?api_key=SENTINEL-1", "http://h/p?a=1&access_token=SENTINEL-1", "tskey-api-SENTINEL-1",
    "Bearer SENTINEL-1", "-----BEGIN PRIVATE KEY-----\nSENTINEL",
])
def test_a_url_with_credentials_is_redacted_whatever_its_key(value):
    out = redact_diagnostics({"entry": {"data": {"harmless": value, "list": ["ok", value], "nested": {"zzz": value}}}})
    assert out["entry"]["data"]["harmless"] == REDACTED
    assert out["entry"]["data"]["list"] == ["ok", REDACTED]
    assert out["entry"]["data"]["nested"]["zzz"] == REDACTED
    # the same value inside the (otherwise pattern-free) content document is caught too
    assert redact_diagnostics({"content": {"anything": value}})["content"]["anything"] == REDACTED


def test_plain_urls_and_innocent_text_are_left_alone():
    data = {"entry": {"data": {"note": "http://homeassistant.local:8123/lovelace", "interval": 5, "state": "ok"}}}
    assert redact_diagnostics(data) == data


def test_an_allowed_key_is_readable_but_its_children_are_still_judged():
    out = redact_diagnostics({"entry": {"data": {"gate_sip_listen": {"dial": True, "peer_token": S, "claims": False}}}})
    assert out["entry"]["data"]["gate_sip_listen"] == {"dial": True, "peer_token": REDACTED, "claims": False}


def test_the_content_document_keeps_its_own_key_names():
    content = {"dashboards": {"hall": {"title": "Hall"}}, "overrides": {"endpoint-1a2b3c4d": {"name": "X", "key": "k"}}}
    assert redact_diagnostics({"content": content})["content"] == content


def test_empty_secrets_are_not_dressed_up_as_redacted():
    out = redact_diagnostics({"entry": {"data": {"sip_password": "", "nvr_api_key": None, "host": ""}}})
    assert out["entry"]["data"] == {"sip_password": "", "nvr_api_key": None, "host": ""}


def test_the_former_exact_name_set_still_redacts_what_it_did():
    for key in diag_mod.TO_REDACT:
        assert redact_diagnostics({"entry": {"data": {key: S}}})["entry"]["data"][key] == REDACTED
        assert redact_diagnostics({"content": {key: S}})["content"][key] == REDACTED


# ---- the guard ---------------------------------------------------------------------------------------------------------

# The reviewer's own idea of "looks secret", independent of the module's patterns and deliberately wider.
LOOKS_SECRET = re.compile(
    r"pass|secret|token|key|auth|credential|cred|login|user|cookie|bearer|session|rtsp|sip_(user|pass)|nvr|private|"
    r"registration|enroll_id|pin$|code$|seed|salt|signature|cert", re.IGNORECASE)

# An allow-listed name that looks secret is only acceptable when it is on this list, each with the reason. Adding to
# ALLOWED_KEYS a name that looks secret, without adding it here, fails the guard.
ACKNOWLEDGED_NON_SECRET = {
    "token_kind": "which kind of HA token a phone holds (llat or refresh), not the token",
    "token_expires_at": "a timestamp",
    "registrations": "a count / the public projection of the push registry rows (no registration value)",
    "registration_updated_at": "a timestamp",
}

FLOW_FILES = ["config_flow.py", "push_flow.py", "phone_flow.py", "enrollment_flow.py", "door/flow.py",
              "door/gate_sip_listen.py", "door/drivers/dahua_sip.py", "door/drivers/unifi_protect.py",
              "phones_commands.py"]
_MARKERS = {"Required", "Optional", "marker"}


def _module_of(rel: str):
    return importlib.import_module("custom_components.portico." + rel[:-3].replace("/", "."))


def _literal(node: ast.AST, module) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        value = getattr(module, node.id, None)
        if not isinstance(value, str):        # imported inside a function: look in the package's modules
            value = next((getattr(m, node.id) for n, m in sys.modules.items()
                          if n.startswith("custom_components.portico") and isinstance(getattr(m, node.id, None), str)),
                         None)
        return value if isinstance(value, str) else None
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        owner = getattr(module, node.value.id, None)
        if owner is None:                     # imported inside a function (`from . import remote_cfg`)
            owner = sys.modules.get(f"custom_components.portico.door.{node.value.id}")
        value = getattr(owner, node.attr, None)
        return value if isinstance(value, str) else None
    return None


def _collect_field_names() -> tuple[set[str], list[str]]:
    """Every first argument of `vol.Required(...)` / `vol.Optional(...)` in the flow files: a string literal, a module
    constant (`F_SA_FILE`, `const.CONF_ENABLED`), or, inside a helper (`_secret_key(name, ...)`), the literals its call
    sites pass. Anything it cannot resolve is returned in the second list, and the test fails on it."""
    names: set[str] = set()
    unresolved: list[str] = []
    for rel in FLOW_FILES:
        module = _module_of(rel)
        tree = ast.parse((PKG / rel).read_text())
        helpers: dict[str, str] = {}      # a helper's first parameter name -> the helper's name
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.args.args:
                helpers.setdefault(fn.args.args[0].arg, fn.name)
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not call.args:
                continue
            func = call.func
            marker = (func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else "")
            if marker not in _MARKERS or (isinstance(func, ast.Attribute) and not (
                    isinstance(func.value, ast.Name) and func.value.id == "vol")):
                continue
            first = call.args[0]
            name = _literal(first, module)
            if name is None and isinstance(first, ast.Name) and first.id in helpers:
                helper = helpers[first.id]
                sites = [c.args[0] for c in ast.walk(tree) if isinstance(c, ast.Call) and c.args
                         and isinstance(c.func, ast.Name) and c.func.id == helper]
                resolved = {_literal(s, module) for s in sites}
                if sites and None not in resolved:
                    names |= resolved           # type: ignore[arg-type]
                    continue
            if name is None:
                unresolved.append(f"{rel}:{call.lineno}")
            else:
                names.add(name)
    from custom_components.portico.door.drivers import DRIVERS

    for meta in DRIVERS.values():
        names |= {str(getattr(k, "schema", k)) for k in meta.config_schema.schema}
    from custom_components.portico.phone_entry import PhoneRecord
    from dataclasses import fields

    names |= {f.name for f in fields(PhoneRecord)}
    return names, unresolved


def _survives(name: str) -> bool:
    """True when a value stored under `name` comes out of the download unchanged."""
    out = redact_diagnostics({"entry": {"data": {name: S}, "options": {name: S}}})
    return out["entry"]["data"][name] == S and out["entry"]["options"][name] == S


def test_the_collector_sees_the_fields_it_must():
    """A collector that finds nothing would pass the guard for ever."""
    names, unresolved = _collect_field_names()
    assert not unresolved, f"cannot resolve the field name at {unresolved}: teach _collect_field_names"
    for must in ("sip_password", "rtsp_password", "nvr_api_key", "tailscale_api_key", "api_access_token",
                 "service_account_json", "registration_string", "open_relay_action", "existing_camera_entity",
                 "ha_dial", "enroll_id", "host", "sip_user"):
        assert must in names, must


def _uncovered(names) -> list[str]:
    """The secret-looking names a download would show: they survive the rule, and are not allow-listed AND
    acknowledged."""
    return sorted(n for n in names if LOOKS_SECRET.search(n) and _survives(n) and (
        n not in ALLOWED_KEYS or n not in ACKNOWLEDGED_NON_SECRET))


def test_every_secret_looking_flow_field_is_redacted_or_acknowledged_non_secret():
    names, unresolved = _collect_field_names()
    assert not unresolved, f"cannot resolve the field name at {unresolved}: teach _collect_field_names"
    uncovered = _uncovered(names)
    assert not uncovered, (
        f"config-flow field(s) {uncovered} look like secrets but diagnostics would show them: add a pattern to "
        "SECRET_KEY_PATTERNS in diagnostics.py (or, if truly not secret, to ALLOWED_KEYS and ACKNOWLEDGED_NON_SECRET)")


def test_every_acknowledged_name_is_allowed_and_every_allowed_secret_lookalike_is_acknowledged():
    assert set(ACKNOWLEDGED_NON_SECRET) <= ALLOWED_KEYS
    unreviewed = sorted(k for k in ALLOWED_KEYS if LOOKS_SECRET.search(k) and k not in ACKNOWLEDGED_NON_SECRET)
    assert not unreviewed, f"ALLOWED_KEYS has secret-looking name(s) nobody acknowledged: {unreviewed}"


def test_the_guard_catches_a_new_secret_looking_field():
    """The guard itself: a new field nobody taught the rule is reported; one the rule covers is not."""
    assert _uncovered({"client_cert", "signing_seed"}) == ["client_cert", "signing_seed"]
    assert _uncovered({"webhook_secret", "fcm_token", "sip_password", "interval", "driver"}) == []
    # allow-listing a secret-looking name without acknowledging it is also reported
    assert _uncovered({"token_kind"}) == []
