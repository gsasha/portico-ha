"""The "Push ring notification (Firebase)" config-flow branch and its options flow
(sender design §2.3, §3.3, §5.2, §6; decisions 1-2 of §12).

Add Integration -> Portico -> "Push ring notification (Firebase push)" creates the one push
entry (unique id `push`) beside the device entries. The installer uploads, or
pastes, the service-account JSON. Recommended: a dedicated service account
with only the Firebase Cloud Messaging role; the console's default
`firebase-adminsdk` key works and is the quick path (decision 2). Optionally
also the app's `google-services.json`, from which the four Firebase client
values are extracted (not secret; for the enrollment QR, Phase P3).

Validation: §2.3's checks, then ONE live token fetch at Google (no message
is sent). The key is then written to the 0600 private Store only; the entry
keeps `project_id`, `client_email`, `private_key_id`, the package and the four
client values. The JSON is never logged, never put in an error message and
never offered back as a form default.

Options (the push entry's Configure): the global switch, the ring window and
"New phones get the push ring notification" (P2 plan G2, decision Q3: ON for a fresh
install, set OFF on the estate; missing on an older entry means OFF), the
per-phone switches and removal, replacing the key (rotation), and "Add a
phone's registration" (offline registration, `reg_string_spec.md`): the admin
pastes the string a phone shows when it cannot reach Home Assistant, and the
phone is registered exactly as its own `portico/push/register` would.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector

from .enrollment_flow import PorticoOptionsFlow
from .push import const, get_manager
from .push.credentials import (
    CredentialError,
    CredentialStore,
    GoogleServicesError,
    ServiceAccount,
    parse_google_services,
    parse_service_account,
)
from .push.oauth import AccessTokenSource, OAuthError
from .push.reg_string import RegStringError
from .push.reg_string import parse as parse_reg_string
from .push.registration import RegistrationRefused, async_find_phone_by_enroll_id, async_register_phone

_LOGGER = logging.getLogger(__name__)

F_SA_FILE = "service_account_file"
F_SA_JSON = "service_account_json"
F_GS_FILE = "google_services_file"
F_GS_JSON = "google_services_json"
F_REGISTRATION = "registration_string"

_JSON_FILE = selector.FileSelector(selector.FileSelectorConfig(accept=".json,application/json"))
_PASTE = selector.TextSelector(selector.TextSelectorConfig(multiline=True))


def _credentials_schema(*, with_google_services: bool, with_settings: bool = False) -> vol.Schema:
    fields: dict = {
        vol.Optional(F_SA_FILE): _JSON_FILE,
        vol.Optional(F_SA_JSON): _PASTE,
    }
    if with_google_services:
        fields[vol.Optional(F_GS_FILE)] = _JSON_FILE
        fields[vol.Optional(F_GS_JSON)] = _PASTE
    if with_settings:
        fields[vol.Required(const.CONF_NEW_PHONES_ENABLED, default=const.DEFAULT_NEW_PHONES_ENABLED)] = bool
    return vol.Schema(fields)


async def _read_upload(hass: HomeAssistant, file_id: str) -> str:
    """The uploaded file's text (HA's file_upload; the file is deleted after)."""
    from homeassistant.components.file_upload import process_uploaded_file

    def _read() -> str:
        with process_uploaded_file(hass, file_id) as path:
            return path.read_text(encoding="utf-8")

    return await hass.async_add_executor_job(_read)


async def _source(hass: HomeAssistant, user_input: dict, file_field: str, paste_field: str) -> tuple[str, str] | None:
    """(field, text) from the upload, else the paste; None when neither."""
    if user_input.get(file_field):
        try:
            return file_field, await _read_upload(hass, user_input[file_field])
        except (ValueError, OSError, UnicodeDecodeError):
            return file_field, ""
    if (user_input.get(paste_field) or "").strip():
        return paste_field, user_input[paste_field]
    return None


async def _async_validate_account(
    hass: HomeAssistant, user_input: dict, errors: dict[str, str]
) -> ServiceAccount | None:
    """§2.3: parse and check the service account, then one live token fetch."""
    source = await _source(hass, user_input, F_SA_FILE, F_SA_JSON)
    if source is None:
        errors["base"] = "service_account_required"
        return None
    field, text = source
    try:
        account = parse_service_account(text)
    except CredentialError as err:
        errors[field] = f"sa_{err.code}"
        return None
    tokens = AccessTokenSource(hass, account)
    try:
        await tokens.async_get_token()
    except OAuthError as err:
        if err.code == "credentials_invalid":
            errors[field] = "sa_rejected"
        else:
            errors["base"] = "cannot_connect"
        _LOGGER.info("portico push: the service account's token check failed: %s", err.code)
        return None
    finally:
        tokens.async_stop()
    return account


async def _async_validate_google_services(
    hass: HomeAssistant, user_input: dict, account: ServiceAccount | None, errors: dict[str, str]
) -> dict[str, str] | None:
    source = await _source(hass, user_input, F_GS_FILE, F_GS_JSON)
    if source is None:
        return None
    field, text = source
    try:
        values = parse_google_services(text, const.PORTICO_PACKAGE)
    except GoogleServicesError as err:
        errors[field] = f"gs_{err.code}"
        return None
    if account is not None and values["project_id"] != account.project_id:
        errors[field] = "gs_project_mismatch"
        return None
    return values


_KIOSK_URL_RE = re.compile(r"(?:https?://|/(?![/\\]))[^\s\"'()\\\x00-\x1f\x7f]+")
_KIOSK_PATH_RE = re.compile(r"/(?![/\\])[^\s\\\x00-\x1f\x7f]*")


def _valid_kiosk_background_url(value: str) -> bool:
    """The kiosk page's own test (portico-panel.js `kioskLook`): an http(s) URL or a same-origin path."""
    return _KIOSK_URL_RE.fullmatch(value) is not None


def _valid_kiosk_back_path(value: str) -> bool:
    """The card's own test (portico-card.js `validBackPath`): one leading "/", at most 512 characters."""
    return len(value) <= 512 and _KIOSK_PATH_RE.fullmatch(value) is not None


class PushConfigFlowMixin:
    """Mixed into PorticoConfigFlow: the push ring notification credentials step of "Add Integration"."""

    async def async_step_push_credentials(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(const.PUSH_UNIQUE_ID)
            self._abort_if_unique_id_configured()
            # Google-services first: a bad optional file should not cost a
            # token round trip.
            firebase = await _async_validate_google_services(self.hass, user_input, None, errors)
            account = None
            if not errors:
                account = await _async_validate_account(self.hass, user_input, errors)
            if account is not None and firebase is not None and firebase["project_id"] != account.project_id:
                errors[F_GS_FILE if user_input.get(F_GS_FILE) else F_GS_JSON] = "gs_project_mismatch"
            if not errors and account is not None:
                await CredentialStore(self.hass).async_save(account)
                data: dict[str, Any] = {
                    const.ENTRY_TYPE: const.ENTRY_TYPE_PUSH,
                    "project_id": account.project_id,
                    "client_email": account.client_email,
                    "private_key_id": account.private_key_id,
                    "package": const.PORTICO_PACKAGE,
                }
                if firebase is not None:
                    data["firebase"] = firebase
                return self.async_create_entry(
                    title="Portico push ring notification",
                    data=data,
                    # CONF_NEW_PHONES_ENABLED is always stored, so "missing" can
                    # keep meaning "an entry from before the option" (OFF),
                    # const.new_phones_enabled. No phone can have registered
                    # before this entry exists, so the test allow-list starts
                    # empty; it is edited under Configure, "Test phones".
                    options={
                        const.CONF_ENABLED: True,
                        const.CONF_RING_WINDOW: const.DEFAULT_RING_WINDOW,
                        const.CONF_NEW_PHONES_ENABLED: bool(user_input.get(
                            const.CONF_NEW_PHONES_ENABLED, const.DEFAULT_NEW_PHONES_ENABLED)),
                        const.CONF_TEST_PHONES: [],
                    },
                )
        return self.async_show_form(
            step_id="push_credentials",
            data_schema=_credentials_schema(with_google_services=True, with_settings=True),
            errors=errors,
            description_placeholders={"package": const.PORTICO_PACKAGE},
        )


class PushOptionsFlow(PorticoOptionsFlow):
    """The push entry's Configure: settings, phones, key rotation, and (S-1) the enrollment steps
    a device entry's Configure has (it inherits them: they act on `self.config_entry`, which is this entry,
    so the Tailscale answer, `ha_dial` and the new phone's row all land here). The per-door "Away call audio"
    option stays on the device entry: provider_impl reads it from there."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(step_id="init",
                                    menu_options=["push_settings", "push_phones", "push_test_phones", "push_key",
                                                  "add_phone_registration", "enroll_phone", "tailscale_change_mode"])

    async def async_step_enroll_phone(self, user_input: dict[str, Any] | None = None):
        """"Enroll a device": the Tailscale question first when this entry has no complete answer, then the
        phone form (a device entry's Configure does the same before its menu)."""
        return await self._async_enrollment_gate() or await self.async_step_phone_details()

    def _keep_options(self):
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    async def async_step_push_settings(self, user_input: dict[str, Any] | None = None):
        from .door.wheel import CONF_WHEEL_BASE_URL, valid_wheel_base
        from .editor_commands import CONF_KIOSK_BACK_PATH, CONF_KIOSK_BACKGROUND_URL

        options = self.config_entry.options
        # Optional text options (DEV-836): the form shows the stored value; a blank field removes the option.
        text_checks = {
            CONF_WHEEL_BASE_URL: (valid_wheel_base, "invalid_wheel_base_url"),
            CONF_KIOSK_BACKGROUND_URL: (_valid_kiosk_background_url, "invalid_kiosk_background_url"),
            CONF_KIOSK_BACK_PATH: (_valid_kiosk_back_path, "invalid_kiosk_back_path"),
        }
        errors: dict[str, str] = {}
        shown = user_input if user_input is not None else options
        if user_input is not None:
            new = {**options, **{k: v for k, v in user_input.items() if k not in text_checks}}
            for key, (check, error) in text_checks.items():
                value = str(user_input.get(key) or "").strip()
                if not value:
                    new.pop(key, None)
                elif check(value):
                    new[key] = value
                else:
                    errors[key] = error
            if not errors:
                return self.async_create_entry(title="", data=new)
        fields: dict[Any, Any] = {
            vol.Required(const.CONF_ENABLED, default=shown.get(const.CONF_ENABLED, True)): bool,
            vol.Required(
                const.CONF_RING_WINDOW, default=shown.get(const.CONF_RING_WINDOW, const.DEFAULT_RING_WINDOW)
            ): vol.All(vol.Coerce(int), vol.Range(min=const.MIN_RING_WINDOW, max=const.MAX_RING_WINDOW)),
            vol.Required(const.CONF_NEW_PHONES_ENABLED, default=const.new_phones_enabled(shown)): bool,
        }
        for key in text_checks:
            current = shown.get(key)
            suggested = current if isinstance(current, str) else ""
            fields[vol.Optional(key, description={"suggested_value": suggested})] = str
        return self.async_show_form(step_id="push_settings", data_schema=vol.Schema(fields), errors=errors)

    async def async_step_push_phones(self, user_input: dict[str, Any] | None = None):
        manager = self.config_entry.runtime_data if hasattr(self.config_entry, "runtime_data") else None
        if manager is None:
            return self.async_abort(reason="not_loaded")
        registry = manager.registry
        rows = registry.rows()
        if user_input is not None:
            enabled = set(user_input.get("enabled_phones", []))
            removed = set(user_input.get("remove_phones", []))
            for row in rows:
                if row["reg_id"] in removed:
                    await registry.async_remove(row["reg_id"])
                elif bool(row.get("push_enabled")) != (row["reg_id"] in enabled):
                    await registry.async_set_enabled(row["reg_id"], row["reg_id"] in enabled)
            return self._keep_options()
        labels = {
            row["reg_id"]: (
                f"{row.get('name') or 'phone'}: {row.get('state')}, last ok {row.get('last_ok_at') or 'never'}"
                + (" (not seen for 60 days)" if registry.is_stale(row) else "")
            )
            for row in rows
        }
        schema = vol.Schema({
            vol.Optional("enabled_phones", default=[r["reg_id"] for r in rows if r.get("push_enabled")]):
                cv.multi_select(labels),
            vol.Optional("remove_phones", default=[]): cv.multi_select(labels),
        })
        hint = "" if rows else "No phone has registered yet: open the Portico app on an enrolled phone."
        return self.async_show_form(step_id="push_phones", data_schema=schema,
                                    description_placeholders={"hint": hint, "count": str(len(rows))})

    async def async_step_push_test_phones(self, user_input: dict[str, Any] | None = None):
        """Options menu "Test phones": the staged real-device test's
        allow-list (spec §8.3, G6; empty by default). The only phones
        `portico.push_test` may send to and, while it is set, the only phones a
        real ring may go to (if also switched on)."""
        manager = self.config_entry.runtime_data if hasattr(self.config_entry, "runtime_data") else None
        if manager is None:
            return self.async_abort(reason="not_loaded")
        registry = manager.registry
        if user_input is not None:
            chosen = [reg_id for reg_id in dict.fromkeys(user_input.get(const.CONF_TEST_PHONES, []))
                      if registry.get(reg_id) is not None]
            return self.async_create_entry(title="", data={**self.config_entry.options, const.CONF_TEST_PHONES: chosen})
        labels = {
            row["reg_id"]: f"{row.get('name') or 'phone'}: push ring notification {'on' if row.get('push_enabled') else 'off'}"
            for row in registry.rows()
        }
        schema = vol.Schema({
            vol.Optional(const.CONF_TEST_PHONES, default=manager.test_phones): cv.multi_select(labels),
        })
        return self.async_show_form(step_id="push_test_phones", data_schema=schema)

    async def async_step_push_key(self, user_input: dict[str, Any] | None = None):
        """Rotation (§6): add a new key in the console, upload it here, then
        delete the old one there."""
        errors: dict[str, str] = {}
        if user_input is not None:
            account = await _async_validate_account(self.hass, user_input, errors)
            if account is not None and account.project_id != self.config_entry.data.get("project_id"):
                errors[F_SA_FILE if user_input.get(F_SA_FILE) else F_SA_JSON] = "sa_project_changed"
                account = None
            if account is not None and not errors:
                await CredentialStore(self.hass).async_save(account)
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={**self.config_entry.data, "client_email": account.client_email,
                          "private_key_id": account.private_key_id},
                )
                self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
                return self._keep_options()
        return self.async_show_form(step_id="push_key", data_schema=_credentials_schema(with_google_services=False),
                                    errors=errors)

    async def async_step_add_phone_registration(self, user_input: dict[str, Any] | None = None):
        """Offline registration (the owner, 2026-10-07): a phone that could not
        reach Home Assistant shows a registration string; pasted here, it
        registers that phone as `portico/push/register` would (shared checks
        and upsert, push/registration.py). Only the admin reaches this form.

        Neither the pasted text nor any of its fields is logged, put into an
        error or a placeholder: errors are fixed translation keys, the
        success screen names the phone by its ENROLLED name."""
        errors: dict[str, str] = {}
        if user_input is not None:
            outcome = await _async_add_registration(self.hass, user_input.get(F_REGISTRATION) or "")
            if isinstance(outcome, str):
                errors["base"] = outcome
            else:
                manager, row = outcome
                if not row["push_enabled"]:
                    reason = "phone_registered_off"
                elif manager.test_allow_list and row["reg_id"] not in manager.test_allow_list:
                    reason = "phone_registered_not_test_listed"
                else:
                    reason = "phone_registered_on"
                return self.async_abort(reason=reason, description_placeholders={"phone": row["name"]})
        return self.async_show_form(
            step_id="add_phone_registration",
            data_schema=vol.Schema({vol.Required(F_REGISTRATION): _PASTE}),
            errors=errors,
        )


# RegistrationRefused code -> this form's error key.
_REFUSED_ERRORS = {
    "invalid_registration": "reg_invalid_registration",
    "project_mismatch": "reg_project_mismatch",
    "package_mismatch": "reg_package_mismatch",
}


async def _async_add_registration(hass: HomeAssistant, text: str):
    """(manager, row) on success, else the form's error key. Logs only the
    failure class."""
    try:
        parsed = parse_reg_string(text)
    except RegStringError as err:
        _LOGGER.info("portico push: a pasted registration string was refused (%s)", err.code)
        return f"reg_{err.code}"
    manager = get_manager(hass)
    if manager is None:
        _LOGGER.info("portico push: a pasted registration string was refused (push_not_configured)")
        return "push_not_configured"
    phone = await async_find_phone_by_enroll_id(hass, parsed.enroll_id)
    if phone is None:
        _LOGGER.info("portico push: a pasted registration string was refused (unknown_enrollment)")
        return "reg_unknown_enrollment"
    user_id, name = phone
    try:
        row = await async_register_phone(
            manager, user_id=user_id, name=name, installation_id=parsed.installation_id, kind=parsed.kind,
            registration=parsed.registration, package=parsed.package, fcm_project_id=parsed.fcm_project_id,
            app_version=parsed.app_version, enroll_id=parsed.enroll_id,
        )
    except RegistrationRefused as err:
        _LOGGER.info("portico push: a pasted registration string was refused (%s)", err.code)
        return _REFUSED_ERRORS.get(err.code, "reg_invalid")
    _LOGGER.info("portico push: registered phone %r from a pasted registration string", name)
    return manager, row
