"""The door side's texts in the ONE strings.json (the door steps, the Reconfigure form, the door issues).

strings.json is HA's source file for core integrations; a CUSTOM integration's UI text is read only from
translations/<lang>.json. Keep the English translation identical to strings.json so neither drifts, and make sure
every error/issue key the code raises has text. No HA harness needed. (The push ring notification and enrollment texts are
`test_strings.py`.)"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load(rel: str) -> dict:
    return json.loads((_ROOT / rel).read_text(encoding="utf-8"))


def test_english_translation_matches_strings_json():
    assert _load("translations/en.json") == _load("strings.json")


def test_observer_keys_have_text():
    strings = _load("strings.json")
    data = strings["config"]["step"]["driver_config"]["data"]
    assert {"gate_sip_url", "interval"} <= data.keys()
    assert {"invalid_url", "invalid_interval"} <= strings["config"]["error"].keys()
    issue = strings["issues"]["driver_error"]
    for placeholder in ("{device}", "{driver}", "{message}"):
        assert placeholder in issue["title"] + issue["description"]


def test_every_form_the_flows_show_has_text():
    strings = _load("strings.json")
    assert {"user", "door", "driver_config", "camera_config", "relay_action"} <= strings["config"]["step"].keys()
    assert strings["config"]["step"]["user"]["menu_options"] == {"door": "Add a door", "push_credentials": "Push ring notification (Firebase)",
                                                                         "sip_server_door": "SIP-server door (advanced, not live)",
                                                                         "sip_server_remote": "SIP-server door, remote container (advanced, not live)"}
    assert "away_media" not in strings["options"]["step"]["door_init"].get("data", {})   # the retired option is gone; the door's form has its own step id
    assert "menu_options" in strings["options"]["step"]["init"]                # `init` stays the push entry's menu
    assert {"already_configured", "reconfigure_successful", "reconfigure_not_supported"} <= strings["config"]["abort"].keys()
    assert {"peer_token_required", "invalid_peer_token", "secret_mask_edited", "claims_need_listen",
            "ring_entity_required"} <= strings["config"]["error"].keys()


def test_the_listen_issues_have_text_and_their_placeholders():
    issues = _load("strings.json")["issues"]
    assert {"driver_error", "listen_leg_down", "call_held_too_long", "wheel_too_old", "wheel_install_failed"} <= set(issues)
    for placeholder in ("{device}", "{state}", "{minutes}", "{reason}"):
        assert placeholder in issues["listen_leg_down"]["title"] + issues["listen_leg_down"]["description"]
    assert "{minutes}" in issues["call_held_too_long"]["description"]


def test_nothing_of_the_removed_seam_is_left_in_the_texts():
    """The provider-version issue went with the registry's API-version check, and no text sends anyone to the old
    separate integration."""
    text = json.dumps(_load("strings.json"))
    for word in ("provider_version_mismatch", "Portico Intercom"):
        assert word not in text, word
    assert "provider_version_mismatch" not in _load("strings.json")["issues"]


def test_the_shared_texts_are_unified():
    """The two collisions of the fold (design 1.5): `already_configured` is one text for the door and the push ring notification."""
    strings = _load("strings.json")
    assert "door" in strings["config"]["abort"]["already_configured"] and "push ring notification" in strings["config"]["abort"]["already_configured"]


def test_the_wheel_too_old_issue_names_both_versions():
    """REL-04: the repair issue says what is installed and what is needed (hub.py fills both placeholders)."""
    issue = _load("strings.json")["issues"]["wheel_too_old"]
    for placeholder in ("{installed}", "{required}"):
        assert placeholder in issue["description"]


def test_the_wheel_install_failed_issue_names_the_version_and_the_reason():
    """The lazy install (wheel.py) raises this when pip cannot fetch the pin; doors wait, the rest runs."""
    issue = _load("strings.json")["issues"]["wheel_install_failed"]
    for placeholder in ("{required}", "{reason}"):
        assert placeholder in issue["description"]
