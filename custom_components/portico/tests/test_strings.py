"""strings.json is HA's source file for core integrations; a CUSTOM
integration's UI text is read only from translations/<lang>.json. Keep the
English translation identical to strings.json so neither drifts, and make
sure every error/issue key the code raises has text. No HA harness needed."""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _docs_text() -> str:
    """The operating documentation these tests read: the Tailscale guide and every other page under docs/
    (the text lived in the root README until the documentation was split into docs/)."""
    root = _ROOT.parent.parent
    pages = [root / "README.md", *sorted((root / "docs").rglob("*.md"))]
    return "\n".join(page.read_text(encoding="utf-8") for page in pages)


def _load(rel: str) -> dict:
    return json.loads((_ROOT / rel).read_text(encoding="utf-8"))


def test_english_translation_matches_strings_json():
    assert _load("translations/en.json") == _load("strings.json")


def test_push_test_keys_have_text():
    """G6: the staged-test service, its refusals and the allow-list step."""
    strings = _load("strings.json")
    service = strings["services"]["push_test"]
    assert service["name"] and service["description"] and service["fields"]["phones"]["name"]
    for key in ("push_test_no_allow_list", "push_test_not_allowed", "push_test_no_live_phone",
                "push_test_rate_limited"):
        assert strings["exceptions"][key]["message"]
    step = strings["options"]["step"]["push_test_phones"]
    assert step["data"]["test_phones"] and step["description"]
    assert "push_test_phones" in strings["options"]["step"]["init"]["menu_options"]


def test_services_yaml_declares_push_test():
    text = (_ROOT / "services.yaml").read_text(encoding="utf-8")
    assert text.startswith("push_test:")
    assert "phones:" in text


def test_push_keys_have_text():
    """Every error the push ring notification flows can show, and every push repair issue,
    has text (FCM sender design §2.3, §7)."""
    strings = _load("strings.json")
    credential_codes = {"invalid_json", "not_service_account", "missing_fields", "invalid_project_id",
                        "invalid_key", "token_uri_not_allowed", "rejected"}
    config_errors = strings["config"]["error"]
    for code in credential_codes:
        assert f"sa_{code}" in config_errors
        assert f"sa_{code}" in strings["options"]["error"]
    for code in ("invalid_json", "missing_fields", "package_not_found", "project_mismatch", "invalid_values"):
        assert f"gs_{code}" in config_errors
    assert {"service_account_required", "cannot_connect"} <= config_errors.keys()
    assert "sa_project_changed" in strings["options"]["error"]
    assert "push_credentials" in strings["config"]["step"]
    assert {"init", "push_settings", "push_phones", "push_key"} <= strings["options"]["step"].keys()
    issues = strings["issues"]
    for issue in ("push_credentials_invalid", "push_project_mismatch", "push_payload_rejected", "push_rate_limited"):
        assert issue in issues
    assert "{phone}" in issues["push_project_mismatch"]["title"]


def test_enrollment_no_url_error_has_text():
    """The enrollment flow refuses to mint a QR without an HA URL; the error
    must tell the installer where to set one."""
    strings = _load("strings.json")
    text = strings["options"]["error"]["no_ha_url"]
    assert "Settings > System > Network" in text
    assert "external" in text and "internal" in text


def test_new_phones_option_has_text():
    """G2 / decision Q3: the option is labelled in the config flow and in the
    options flow."""
    strings = _load("strings.json")
    label = "New phones get the push ring notification"
    assert strings["config"]["step"]["push_credentials"]["data"]["new_phones_enabled"] == label
    assert strings["options"]["step"]["push_settings"]["data"]["new_phones_enabled"] == label


def test_enrollment_https_choice_has_text():
    """S8: the plain-http checkbox says the connection is not encrypted, and
    the no-https refusal says where to set an https URL or to tick the box."""
    strings = _load("strings.json")
    step = strings["options"]["step"]["phone_details"]
    assert step["data"]["allow_http"] == "Allow plain http on my own network"
    explain = step["data_description"]["allow_http"]
    assert "not encrypted" in explain and "trusted" in explain
    text = strings["options"]["error"]["no_https_url"]
    assert "https" in text and "Settings > System > Network" in text
    assert "Allow plain http on my own network" in text


def test_enrollment_fcm_skipped_step_has_text():
    """S8: the QR shown when the push ring notification values were left out warns, and
    still carries every placeholder the normal QR step has."""
    strings = _load("strings.json")
    steps = strings["options"]["step"]
    skipped = steps["show_qr_fcm_skipped"]["description"]
    for placeholder in ("{qr_image}", "{phone_name}", "{manual_note}", "{ha_dial}"):
        assert placeholder in steps["show_qr"]["description"]
        assert placeholder in skipped
    assert "push ring notification" in skipped and "google-services.json" in skipped


def test_enrollment_mint_failure_error_has_text():
    """A failed Tailscale key mint names the credential to check and the way
    out (another mode, under Configure)."""
    strings = _load("strings.json")
    text = strings["options"]["error"]["tailscale_mint_failed"]
    assert "Tailscale" in text and "API access token" in text and "OAuth client secret" in text and "tailnet" in text
    assert "manual" in text


def test_enrollment_mode_labels_are_translated():
    """DEV-556: the mode question's three choices, approve first, each with a
    label under the selector's translation key."""
    strings = _load("strings.json")
    options = strings["selector"]["tailscale_mode"]["options"]
    assert list(options) == ["approve", "auto", "manual"]
    assert all(isinstance(v, str) and v.strip() for v in options.values())
    assert "approve" in options["approve"].lower()


def test_enrollment_approve_steps_have_text():
    """DEV-556: the one-time OAuth-app steps show the redirect URI to register,
    the approval step tells the installer what to do if Tailscale refuses, and
    every error the mode can raise has text that names a way out."""
    strings = _load("strings.json")
    steps = strings["options"]["step"]
    choice = steps["tailscale_oauth_app_choice"]
    assert set(choice["menu_options"]) == {"tailscale_oauth_app", "tailscale_oauth_app_create"}
    for step_id in ("tailscale_oauth_app_choice", "tailscale_oauth_app", "tailscale_oauth_app_create"):
        assert "{redirect_uri}" in steps[step_id]["description"]
        assert "auth_keys:create:once" in steps[step_id]["description"]
    assert {"client_secret", "client_id"} <= steps["tailscale_oauth_app"]["data"].keys()
    assert "api_access_token" in steps["tailscale_oauth_app_create"]["data"]
    assert "not stored" in steps["tailscale_oauth_app_create"]["description"]
    approve = steps["tailscale_approve"]["description"]
    assert "Tailscale" in approve and "another mode" in approve
    errors = strings["options"]["error"]
    for key in ("tailscale_client_secret_invalid", "tailscale_client_id_invalid", "tailscale_api_token_rejected",
                "tailscale_create_app_failed", "tailscale_approval_denied", "tailscale_exchange_failed"):
        assert errors[key].strip()
    rejected = errors["tailscale_exchange_rejected"]
    assert "OAuth app" in rejected and "redirect URI" in rejected and "another mode" in rejected
    assert "Change the Tailscale enrollment mode" in rejected


def test_enrollment_qr_step_says_the_key_lasts_an_hour():
    """A QR-delivered Tailscale key (auto or approve) is single-use and lasts
    one hour: both QR steps say so."""
    steps = _load("strings.json")["options"]["step"]
    for step_id in ("show_qr", "show_qr_fcm_skipped"):
        text = steps[step_id]["description"]
        assert "one hour" in text and "single-use" in text


def test_offline_registration_has_text():
    """Offline registration (Alice, 2026-10-07): both QR steps say the phone
    must reach Home Assistant when it scans and where to paste its
    registration string otherwise; the phone-details step hints at it; the
    paste step, its menu entry, every refusal and the success screens have
    text."""
    strings = _load("strings.json")
    steps = strings["options"]["step"]
    label = "Add a phone's registration"
    for step_id in ("show_qr", "show_qr_fcm_skipped"):
        text = steps[step_id]["description"]
        assert "reach this Home Assistant" in text and "Wi-Fi" in text
        assert "registration string" in text and label in text and "Push ring notification" in text
    assert "reach Home Assistant" in steps["phone_details"]["description"]
    assert steps["init"]["menu_options"]["add_phone_registration"] == label
    paste = steps["add_phone_registration"]
    assert paste["title"] == label and paste["data"]["registration_string"] and paste["description"]
    errors = strings["options"]["error"]
    for key in ("reg_not_a_string", "reg_damaged", "reg_version", "reg_invalid", "reg_too_long",
                "reg_project_mismatch", "reg_package_mismatch", "reg_invalid_registration",
                "reg_unknown_enrollment", "push_not_configured"):
        assert errors[key].strip(), key
    assert "another Firebase project" in errors["reg_project_mismatch"]
    assert "enroll" in errors["reg_unknown_enrollment"]
    aborts = strings["options"]["abort"]
    for key in ("phone_registered_on", "phone_registered_off", "phone_registered_not_test_listed"):
        assert "{phone}" in aborts[key]


def test_enrollment_ha_dial_has_text():
    """DEV-557: the optional Tailscale address field says blank is automatic
    and what a value looks like; its error says what is accepted; the QR page
    names the address in one line."""
    strings = _load("strings.json")
    step = strings["options"]["step"]["phone_details"]
    assert step["data"]["ha_dial"] == "Address phones use over Tailscale"
    explain = step["data_description"]["ha_dial"]
    assert "blank" in explain.lower() and "automatic" in explain.lower()
    assert "10.6.0.5:443" in explain and "Tailscale" in explain
    error = strings["options"]["error"]["invalid_ha_dial"]
    assert "10.6.0.5:443" in error and "private" in error and "port" in error
    lines = [line for line in strings["options"]["step"]["show_qr"]["description"].split("\n") if "{ha_dial}" in line]
    assert len(lines) == 1 and "Tailscale" in lines[0]


def test_enrollment_change_mode_has_text():
    """Alice, 2026-10-08: the device entry's Configure menu, the change
    question, the re-setup of an OAuth app whose redirect URI went stale, and
    the three confirmations; no text sends the installer to remove and re-add
    the entry to change the mode any more."""
    strings = _load("strings.json")
    steps = strings["options"]["step"]
    menu = steps["init"]["menu_options"]
    assert {"enroll_phone", "tailscale_change_mode"} <= set(menu)
    assert not {"content_phone", "home_name"} & set(menu)   # E7: the panel is the only content editor
    assert menu["enroll_phone"] == "Enroll a device"
    assert menu["tailscale_change_mode"] == "Change the Tailscale enrollment mode"
    change = steps["tailscale_change_mode"]
    assert change["title"] and change["description"] and change["data"]["tailscale_mode"]
    stale = steps["tailscale_oauth_app_choice_redirect_changed"]
    assert set(stale["menu_options"]) == {"tailscale_oauth_app", "tailscale_oauth_app_create"}
    assert stale["menu_options"] == steps["tailscale_oauth_app_choice"]["menu_options"]
    for placeholder in ("{redirect_uri}", "{old_redirect_uri}", "auth_keys:create:once"):
        assert placeholder in stale["description"]
    assert "changed" in stale["description"]
    aborts = strings["options"]["abort"]
    for mode in ("approve", "auto", "manual"):
        assert aborts[f"tailscale_mode_saved_{mode}"].strip()
    label = "Change the Tailscale enrollment mode"
    for text in (steps["tailscale_approve"]["description"], steps["tailscale_mode"]["description"],
                 strings["options"]["error"]["tailscale_exchange_rejected"],
                 strings["options"]["error"]["tailscale_mint_failed"]):
        assert label in text
    tailscale_text = json.dumps({k: v for k, v in steps.items() if k.startswith(("tailscale", "init"))})
    tailscale_text += json.dumps({k: v for k, v in strings["options"]["error"].items() if k.startswith("tailscale")})
    assert "re-add" not in tailscale_text


def test_manual_mode_promises_no_key_field_in_the_app():
    """portico-app has no field to type a Tailscale key: a key reaches it only
    through the QR. `manual` says the phone does not join the tailnet through
    Portico and that the away path needs approve or auto."""
    strings = _load("strings.json")
    everything = json.dumps(strings)
    assert "Setup screen" not in everything and "paste a key" not in everything.lower()
    question = strings["options"]["step"]["tailscale_mode"]["description"]
    assert "no Tailscale key" in question and "official Tailscale app" in question
    assert "does not join" in question
    label = strings["selector"]["tailscale_mode"]["options"]["manual"]
    assert "set up Tailscale on each phone myself" not in label


def test_readme_has_no_remove_and_readd_advice_for_the_mode():
    readme = _docs_text()
    assert "Change the Tailscale enrollment mode" in readme
    assert "remove and re-add the device entry" not in readme
    assert "Setup screen" not in readme


def test_auto_mode_texts_follow_tailscale_docs():
    """The `auto` credential page (2026-10-08): an OAuth client secret is
    exchanged, not used as a bearer; the scope is `auth_keys`, made under
    Trust credentials; an API access token expires; the node is not
    ephemeral, so the QR page says to remove its machine by hand."""
    strings = _load("strings.json")
    steps = strings["options"]["step"]
    creds = steps["tailscale_credentials"]["description"]
    for gone in ("Either one works", "devices:core", "Settings > OAuth clients"):
        assert gone not in creds
    for needed in ("Trust credentials", "`auth_keys`", "Write", "tskey-client-", "tskey-api-",
                   "expires", "tagOwners", "10.6.0.5:443", "check your console"):
        assert needed in creds, needed
    assert "Approve in Tailscale" in creds
    assert "OAuth client" in steps["tailscale_mode"]["description"]
    for qr_step in ("show_qr", "show_qr_fcm_skipped"):
        text = steps[qr_step]["description"]
        assert "Machines" in text and "admin console" in text, qr_step


def test_auto_credentials_texts_name_the_tag_rule():
    """2026-10-08: the installer left the old intercom app's tag on the form
    and Tailscale refused every key. The credentials step says the tag must be
    one the OAuth client was created with (and exist in `tagOwners`), not to
    reuse another app's tag, how a stored secret is kept; the new default is
    `tag:portico-phone`; `intercom-phone` is recommended nowhere."""
    strings = _load("strings.json")
    step = strings["options"]["step"]["tailscale_credentials"]
    text = step["description"]
    assert "OAuth client was created with" in text and "tagOwners" in text
    assert "another app's tag" in text
    assert "tag:portico-phone" in text
    secret_help = step["data_description"]["tailscale_api_key"]
    assert "blank" in secret_help and "stored" in secret_help
    assert step["data_description"]["tailscale_tag"].strip()
    errors = strings["options"]["error"]
    for key in ("tailscale_tag_invalid", "tailscale_tag_blank", "tailscale_tailnet_blank", "tailscale_api_key_blank"):
        assert errors[key].strip(), key
    assert "tag:" in errors["tailscale_tag_invalid"]
    readme = _docs_text()
    assert "tag:portico-phone" in readme and "another app's tag" in readme
    for source in (json.dumps(strings), readme):
        assert "intercom-phone" not in source


STARS = "stars mean a secret is already stored; leave them to keep it, or type a new one to replace it"


def test_stored_secret_stars_are_explained():
    """2026-10-09: a stored secret shows as a row of stars. The auto step and
    the approve secret step say what the stars mean; editing them is refused
    with its own message. The one-time admin token is stored nowhere, so its
    step never mentions stars."""
    strings = _load("strings.json")
    steps = strings["options"]["step"]
    creds = steps["tailscale_credentials"]
    assert STARS in creds["description"]
    assert STARS in creds["data_description"]["tailscale_api_key"]
    app = steps["tailscale_oauth_app"]
    assert STARS in app["description"]
    assert STARS in app["data_description"]["client_secret"]
    assert "stars" not in json.dumps(steps["tailscale_oauth_app_create"])
    assert "stars" in steps["tailscale_oauth_app_choice_redirect_changed"]["description"]
    edited = strings["options"]["error"]["secret_mask_edited"]
    assert "stars" in edited and "clear" in edited.lower()
    readme = _docs_text()
    assert STARS in readme


def test_the_old_format_door_entry_text_exists():
    """The abort an old-format door entry's Configure shows (`data.driver`, no `entry_type`): delete it, add the door
    again. It no longer names a separate integration."""
    strings = _load("strings.json")
    abort = strings["options"]["abort"]["door_entry_moved"]
    assert "old-format" in abort and "Add a door" in abort and "Portico Intercom" not in abort
    assert strings["config"]["abort"]["already_configured"]


def test_settings_text_options_have_text():
    """DEV-836: the three optional settings fields have a label, a description and an error text."""
    strings = _load("strings.json")
    step = strings["options"]["step"]["push_settings"]
    for key, error in (("wheel_base_url", "invalid_wheel_base_url"),
                       ("kiosk_background_url", "invalid_kiosk_background_url"),
                       ("kiosk_back_path", "invalid_kiosk_back_path")):
        assert step["data"][key] and step["data_description"][key]
        assert strings["options"]["error"][error]
