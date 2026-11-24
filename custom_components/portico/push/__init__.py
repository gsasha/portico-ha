"""Portico push ring notification: the FCM push sender (sender design
`portico_fcm_sender_design.md`, Phase P1).

PushManager is the domain singleton in `hass.data[DOMAIN]["push"]`, created
by the push config entry (`entry_type: push`, unique id `push`) and gone when
that entry unloads. With no push entry nothing here is loaded and the ring
path is the WebSocket alone (§7).

It is inert until a service account is configured AND a portico-app phone has
registered over `portico/push/register` AND that phone is switched on: a new
phone starts on or off by the entry's "New phones get the push ring notification" option
(ON for a fresh install, OFF for an entry that predates it; this estate sets
it OFF, decision 4), and the owner can switch each phone afterwards.

The staged real-device test (spec §8.3, P2 plan G6): the admin-only service
`portico.push_test` sends one `kind: "test"` message to each phone on the
entry's test allow-list (option `test_phones`, empty by default), whatever
that phone's push ring notification switch says, and refuses anything else. While the
allow-list is set, a real ring goes only to switched-on phones on it.

It pushes only to portico-app registrations in its own registry: never to the
old intercom-app's `input_text.intercom_fcm_token_*` helpers, and it never
reads or writes any `input_text` (§0.2: writing `input_text.intercom_call_id`
would ring the house twice).
"""
from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.auth import EVENT_USER_REMOVED
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util import dt as dt_util

from . import const
from .credentials import ServiceAccount
from .fcm import FcmClient, Outcome, SendResult
from .notifier import RingNotifier
from ..provider import DATA_PROVIDERS, ProviderRegistry
from .oauth import AccessTokenSource, OAuthError
from .payload import KIND_TEST
from .registry import PushRegistry

_LOGGER = logging.getLogger(__name__)

DOMAIN = "portico"
_WARN_EVERY = 600.0  # one transient-failure WARNING per 10 minutes (§2.4)
_DAY = 86400.0

STATE_OK = "ok"
STATE_DEGRADED = "degraded"
STATE_CREDENTIALS_INVALID = "credentials_invalid"
STATE_DISABLED = "disabled"


def get_manager(hass: HomeAssistant) -> PushManager | None:
    data = getattr(hass, "data", None)
    if not isinstance(data, dict):
        return None
    return data.get(DOMAIN, {}).get(const.DATA_PUSH)


class PushManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, account: ServiceAccount,
                 registry: PushRegistry) -> None:
        self.hass = hass
        self.entry = entry
        self.account = account
        self.registry = registry
        self.tokens = AccessTokenSource(hass, account, on_error=self._on_oauth_error,
                                        on_success=self._on_oauth_ok)
        self.client = FcmClient(hass, self.tokens, account.project_id)
        self.notifier = RingNotifier(hass, registry, self.client, ring_window_s=lambda: self.ring_window,
                                     enabled=lambda: self.enabled, events=self,
                                     test_phones=lambda: self.test_allow_list,
                                     wire_id=self._wire_id)
        self._unsub_transitions: Callable[[], None] | None = None
        self._providers: ProviderRegistry | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []
        self._credentials_invalid = False
        self._degraded = False
        self._last_warning = 0.0
        self._circuit_open = False
        self._payload_issue = False
        self.last_send_at: str | None = None
        self.last_call_id: str | None = None
        self._sent: deque[float] = deque()
        self._failed: deque[float] = deque()
        self.counters = {"sent": 0, "ok": 0, "dead": 0, "failed": 0, "skipped_device_cap": 0,
                         "skipped_rate_limited": 0, "test": 0}

    def _wire_id(self, door_id: str) -> str | None:
        """The push `door_ref` of a door (push/v2): `Door.door_ref`, derived from the door's stable endpoint id, read
        through the provider registry. None for a door that is not (or no longer) known."""
        registry = self.hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)
        found = registry.door(door_id) if registry is not None else None
        return found[1].door_ref if found is not None else None

    # ---- house calls (M7 3c): what the house-call registry asks of the push ring notification -----------------------------------

    def house_has_push(self, user_id: str | None) -> bool:
        """Whether a house ring can reach this HA user by notification: the push ring notification is on, its credentials are not known
        bad, and the user has a switched-on live registration (and is on the staged-test list while one is set)."""
        return self.enabled and not self._credentials_invalid and bool(self.notifier.house_targets(user_id))

    def house_ring(self, call_id: str, from_ref: str, callees: dict[str, str], ring_s: int) -> set[str]:
        return self.notifier.house_ring(call_id, from_ref, callees, ring_s)

    def house_answered(self, call_id: str, winner: str) -> None:
        self.notifier.house_answered(call_id, winner)

    def house_left(self, call_id: str, endpoint_id: str, *, notify: bool) -> None:
        self.notifier.house_left(call_id, endpoint_id, notify=notify)

    def house_ended(self, call_id: str) -> None:
        self.notifier.house_ended(call_id)

    # ---- options -----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.entry.options.get(const.CONF_ENABLED, True))

    @property
    def new_phones_enabled(self) -> bool:
        return const.new_phones_enabled(self.entry.options)

    @property
    def ring_window(self) -> int:
        return int(self.entry.options.get(const.CONF_RING_WINDOW, const.DEFAULT_RING_WINDOW))

    @property
    def test_allow_list(self) -> list[str]:
        """The stored allow-list as is. Real rings are filtered by THIS list,
        so a listed phone that has since gone (its row removed, or a
        reinstall minted a new one) keeps rings restricted until the list is
        saved again: a staged test fails closed, never open to the household."""
        stored = self.entry.options.get(const.CONF_TEST_PHONES) or []
        return list(dict.fromkeys(str(reg_id) for reg_id in stored))

    @property
    def test_phones(self) -> list[str]:
        """The allow-listed phones that still exist (what `push_test` and the
        options form use)."""
        return [reg_id for reg_id in self.test_allow_list if self.registry.get(reg_id) is not None]

    @property
    def package(self) -> str:
        return self.entry.data.get("package") or const.PORTICO_PACKAGE

    @property
    def project_id(self) -> str:
        return self.account.project_id

    # ---- lifecycle ---------------------------------------------------------

    async def async_start(self) -> None:
        await self.registry.async_load()
        await self.registry.async_age()
        self.registry.add_listener(self._notify)
        self._unsubs.append(self.hass.bus.async_listen(EVENT_USER_REMOVED, self._user_removed))
        self._unsubs.append(async_track_time_interval(
            self.hass, self._daily, timedelta(hours=24), name="portico push registry aging",
            cancel_on_shutdown=True))
        # Prefetch the access token in the background, so neither HA start
        # nor the first ring waits for Google.
        self.entry.async_create_background_task(self.hass, self.tokens.async_start(), "portico push token prefetch")

    async def async_stop(self) -> None:
        self.unsubscribe_transitions()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self.registry.remove_listener(self._notify)
        self.notifier.async_stop()
        self.tokens.async_stop()

    @callback
    def subscribe_transitions(self, registry: ProviderRegistry) -> None:
        """PUSH-02: follow every door's call transitions through the provider registry (one subscription,
        whatever doors come and go), not each door entry's tracker."""
        self.unsubscribe_transitions()
        self._providers = registry
        self._unsub_transitions = registry.subscribe(self.notifier.handle_transition)

    @callback
    def unsubscribe_transitions(self) -> None:
        if self._unsub_transitions is not None:
            self._unsub_transitions()
            self._unsub_transitions = None
        self._providers = None

    async def _user_removed(self, event: Event) -> None:
        user_id = event.data.get("user_id")
        if user_id and await self.registry.async_remove_user(user_id):
            _LOGGER.info("portico push: removed the registrations of a deleted user")

    async def _daily(self, _now) -> None:
        await self.registry.async_age()

    # ---- state for the sensor ------------------------------------------------

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()
        # The per-phone sensors (P-4) read this manager's registry; they follow it through the dispatcher so a
        # reload of the push ring notification never leaves them holding a dead listener.
        async_dispatcher_send(self.hass, const.SIGNAL_PHONES_CHANGED)

    @property
    def state(self) -> str:
        if not self.enabled:
            return STATE_DISABLED
        if self._credentials_invalid:
            return STATE_CREDENTIALS_INVALID
        if self._degraded:
            return STATE_DEGRADED
        return STATE_OK

    def _count(self, window: deque[float]) -> int:
        now = time.monotonic()
        while window and now - window[0] > _DAY:
            window.popleft()
        return len(window)

    def attributes(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        rows = self.registry.rows()
        return {
            "last_send_at": self.last_send_at,
            "last_call_id": self.last_call_id,
            "registrations": len(rows),
            "sent_24h": self._count(self._sent),
            "failed_24h": self._count(self._failed),
            "test_phones": len(self.test_allow_list),
            "devices": [
                {"name": r.get("name"), "state": r.get("state"), "push_enabled": bool(r.get("push_enabled")),
                 "last_result": r.get("last_result"), "last_ok_at": r.get("last_ok_at"),
                 "registration_updated_at": r.get("registration_updated_at"),
                 "stale": PushRegistry.is_stale(r, now)}
                for r in rows
            ],
        }

    def diagnostics(self) -> dict[str, Any]:
        """No registration value, key, token or URL: rows go through the same
        public projection the WebSocket status uses."""
        return {
            "state": self.state,
            "enabled": self.enabled,
            "new_phones_enabled": self.new_phones_enabled,
            "ring_window": self.ring_window,
            "project_id": self.project_id,
            "package": self.package,
            "counters": dict(self.counters),
            "circuit_open": self._circuit_open,
            "attached_devices": len(self._providers.doors()) if self._providers is not None else 0,
            **{k: v for k, v in self.attributes().items() if k != "devices"},
            "registrations": [PushRegistry.public(r) for r in self.registry.rows()],
        }

    # ---- NotifierEvents ------------------------------------------------------

    def on_result(self, row: dict, kind: str, call_id: str, result: SendResult) -> None:
        now = time.monotonic()
        self.counters["sent"] += 1
        self._sent.append(now)
        self.last_send_at = dt_util.utcnow().isoformat()
        if kind == KIND_TEST:
            self.counters["test"] += 1  # a test is not a call: last_call_id stays
        else:
            self.last_call_id = call_id
        outcome = result.outcome
        if outcome is Outcome.OK:
            self.counters["ok"] += 1
            self._set_credentials_invalid(False)
            self._degraded = False
            if self._payload_issue:
                self._payload_issue = False
                ir.async_delete_issue(self.hass, DOMAIN, const.ISSUE_PAYLOAD_REJECTED)
            if not self._circuit_open:
                ir.async_delete_issue(self.hass, DOMAIN, const.ISSUE_RATE_LIMITED)
            ir.async_delete_issue(self.hass, DOMAIN, f"{const.ISSUE_PROJECT_MISMATCH}_{row['reg_id']}")
        elif outcome is Outcome.DEAD:
            self.counters["dead"] += 1
            _LOGGER.info("portico push: registration %s of %r is no longer valid (%s); pruned",
                         row["reg_id"], row.get("name"), result.error)
        else:
            self.counters["failed"] += 1
            self._failed.append(now)
            if outcome in (Outcome.TRANSIENT, Outcome.QUOTA):
                self._degraded = True
                if now - self._last_warning > _WARN_EVERY or not self._last_warning:
                    self._last_warning = now
                    _LOGGER.warning("portico push: sending to Google failed (%s); retrying within the ring "
                                    "deadline. The tailnet ring is unaffected", result.error or outcome.value)
            elif outcome is Outcome.CREDENTIALS_INVALID:
                self._set_credentials_invalid(True, result.error)
            elif outcome is Outcome.PAYLOAD_REJECTED:
                if not self._payload_issue:
                    self._payload_issue = True
                    _LOGGER.error("portico push: Google rejected a %s message (%s); this is a Portico bug",
                                  kind, result.error)
                    ir.async_create_issue(self.hass, DOMAIN, const.ISSUE_PAYLOAD_REJECTED, is_fixable=False,
                                          severity=ir.IssueSeverity.ERROR,
                                          translation_key=const.ISSUE_PAYLOAD_REJECTED,
                                          translation_placeholders={"error": result.error or "unknown"})
            elif outcome is Outcome.PROJECT_MISMATCH:
                _LOGGER.error("portico push: phone %r registered with another Firebase project", row.get("name"))
                ir.async_create_issue(self.hass, DOMAIN, f"{const.ISSUE_PROJECT_MISMATCH}_{row['reg_id']}",
                                      is_fixable=False, severity=ir.IssueSeverity.WARNING,
                                      translation_key=const.ISSUE_PROJECT_MISMATCH,
                                      translation_placeholders={"phone": str(row.get("name") or "a phone")})
        self._notify()

    def on_skip(self, reason: str, door_ref: str | None, call_id: str | None) -> None:
        key = "skipped_device_cap" if reason == "device_rate" else "skipped_rate_limited"
        self.counters[key] += 1

    def on_circuit(self, is_open: bool) -> None:
        self._circuit_open = is_open
        if is_open:
            ir.async_create_issue(self.hass, DOMAIN, const.ISSUE_RATE_LIMITED, is_fixable=False,
                                  severity=ir.IssueSeverity.WARNING, translation_key=const.ISSUE_RATE_LIMITED)
        self._notify()

    def on_summary(self, kind: str, call_id: str, counts: dict[str, int]) -> None:
        pass

    # ---- the staged test (spec §8.3) -------------------------------------------

    def _refuse(self, key: str) -> ServiceValidationError:
        return ServiceValidationError(translation_domain=DOMAIN, translation_key=key)

    async def async_push_test(self, phones: list[str] | None) -> dict[str, Any]:
        """Send one `test` push to the allow-listed phones (all of them, or the
        named subset). Anything not on the allow-list is refused before a
        single message goes out. A phone's push ring notification switch is ignored here."""
        allowed = self.test_phones
        if not allowed:
            raise self._refuse("push_test_no_allow_list")
        if phones:
            wanted = list(dict.fromkeys(phones))
            if any(reg_id not in allowed for reg_id in wanted):
                raise self._refuse("push_test_not_allowed")
        else:
            wanted = allowed
        rows = [row for row in (self.registry.get(reg_id) for reg_id in wanted)
                if row is not None and row.get("registration")]
        if not rows:
            raise self._refuse("push_test_no_live_phone")
        if not self.notifier.take_test_slot():
            raise self._refuse("push_test_rate_limited")
        sent = await self.notifier.async_send_test(rows)
        results = [{"phone": row.get("name") or "phone",
                    "result": result.outcome.value if result is not None else "rate_limited"}
                   for row, result in sent]
        made = sum(1 for _row, result in sent if result is not None)
        _LOGGER.info("portico push: test push to %d allow-listed phone(s): %s", len(rows),
                     ", ".join(sorted(r["result"] for r in results)))
        self._notify()
        return {"sent": made, "results": results}

    # ---- OAuth ---------------------------------------------------------------

    def _on_oauth_error(self, err: OAuthError) -> None:
        if err.code == "credentials_invalid":
            self._set_credentials_invalid(True, err.detail)
        else:
            self._degraded = True
            self._notify()

    def _on_oauth_ok(self) -> None:
        self._set_credentials_invalid(False)

    def _set_credentials_invalid(self, invalid: bool, detail: str = "") -> None:
        if invalid == self._credentials_invalid:
            return
        self._credentials_invalid = invalid
        if invalid:
            _LOGGER.error("portico push: Google refused the service account (%s); upload a valid key in the "
                          "Portico push ring notification entry", detail or "credentials invalid")
            ir.async_create_issue(self.hass, DOMAIN, const.ISSUE_CREDENTIALS_INVALID, is_fixable=False,
                                  severity=ir.IssueSeverity.ERROR, translation_key=const.ISSUE_CREDENTIALS_INVALID,
                                  translation_placeholders={"error": detail or "credentials invalid"})
        else:
            ir.async_delete_issue(self.hass, DOMAIN, const.ISSUE_CREDENTIALS_INVALID)
        self._notify()


# ---- the push config entry ---------------------------------------------------

PUSH_PLATFORMS: list[str] = ["sensor", "binary_sensor"]

PUSH_TEST_SCHEMA = vol.Schema({vol.Optional("phones"): vol.All(cv.ensure_list, [cv.string])})


def _register_push_test(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """`portico.push_test`, admin only, present while the push entry is."""

    async def _handle(call: ServiceCall) -> ServiceResponse:
        manager = get_manager(hass)
        if manager is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="push_test_no_allow_list")
        return await manager.async_push_test(call.data.get("phones"))

    async_register_admin_service(hass, DOMAIN, const.SERVICE_PUSH_TEST, _handle, schema=PUSH_TEST_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    entry.async_on_unload(lambda: hass.services.async_remove(DOMAIN, const.SERVICE_PUSH_TEST))


async def async_setup_push_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load the stored service account, start the manager, and attach it to
    every device entry that is already running (device entries set up later
    attach themselves, __init__.async_setup_entry)."""
    from homeassistant.exceptions import ConfigEntryError

    from ..enrollment_adopt import async_adopt_enrollment
    from ..push_ring_rename import async_migrate_push_ring_names
    from .credentials import CredentialError, CredentialStore

    async_migrate_push_ring_names(hass, entry)      # "away ring" -> "push ring notification" (2026-11-12): before the sensor loads

    # S-1: enrollment gets its second home first, so a dead key cannot keep it from arriving. It is a copy: it
    # may fail loudly in the log, never fail this setup.
    try:
        await async_adopt_enrollment(hass, entry)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("portico push: adopting the enrollment settings onto this entry failed")

    # P-4: the phone rows on this entry become phone entries. A task, not an await: creating an entry sets the
    # domain up, which cannot finish while this setup is still part of it. Idempotent; a failure is logged and the
    # rows stay readable.
    hass.async_create_task(_async_adopt_phones(hass, entry), f"portico adopt phones {entry.entry_id}",
                           eager_start=False)

    try:
        account = await CredentialStore(hass).async_load()
    except CredentialError as err:
        account = None
        code = err.code
    else:
        code = "missing"
    if account is None:
        ir.async_create_issue(hass, DOMAIN, const.ISSUE_CREDENTIALS_INVALID, is_fixable=False,
                              severity=ir.IssueSeverity.ERROR, translation_key=const.ISSUE_CREDENTIALS_INVALID,
                              translation_placeholders={"error": f"stored key {code}"})
        raise ConfigEntryError(f"the stored service account is unusable ({code})")
    # A key loads again: an issue a failed setup raised is stale. A key Google
    # refuses raises it again on the first token fetch.
    ir.async_delete_issue(hass, DOMAIN, const.ISSUE_CREDENTIALS_INVALID)

    # Read at each NEW registration, so an options change applies at once.
    registry = PushRegistry(hass, new_row_enabled=lambda: const.new_phones_enabled(entry.options))
    manager = PushManager(hass, entry, account, registry)
    await manager.async_start()
    hass.data.setdefault(DOMAIN, {})[const.DATA_PUSH] = manager
    entry.runtime_data = manager
    registry_of_providers = hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)
    if registry_of_providers is not None:
        manager.subscribe_transitions(registry_of_providers)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _register_push_test(hass, entry)
    if PUSH_PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PUSH_PLATFORMS)
    manager._notify()  # noqa: SLF001 - the phones' sensors can now read the registry
    return True


async def _async_adopt_phones(hass: HomeAssistant, entry: ConfigEntry) -> None:
    from ..phone_entry import async_adopt_phones

    try:
        await async_adopt_phones(hass, entry)
    except Exception:  # noqa: BLE001 - never fatal: the rows are still read as phones
        _LOGGER.exception("portico push: turning the enrolled-phone rows into phone entries failed")


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    manager = get_manager(hass)
    if manager is not None:
        manager._notify()  # noqa: SLF001 - state (enabled/disabled, test allow-list) follows the options


async def async_unload_push_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if PUSH_PLATFORMS and not await hass.config_entries.async_unload_platforms(entry, PUSH_PLATFORMS):
        return False
    manager = hass.data.get(DOMAIN, {}).pop(const.DATA_PUSH, None)
    if manager is not None:
        await manager.async_stop()
        async_dispatcher_send(hass, const.SIGNAL_PHONES_CHANGED)
    for issue in (const.ISSUE_CREDENTIALS_INVALID, const.ISSUE_PAYLOAD_REJECTED, const.ISSUE_RATE_LIMITED):
        ir.async_delete_issue(hass, DOMAIN, issue)
    return True


async def async_remove_push_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Deleting the push ring notification entry deletes the key and the registry."""
    from homeassistant.helpers.storage import Store

    from .credentials import CredentialStore

    from ..tailscale_approve import async_remove_entry_store

    await CredentialStore(hass).async_remove()
    await Store(hass, 1, const.REGISTRY_STORE_KEY, private=True).async_remove()
    await async_remove_entry_store(hass, entry.entry_id)  # S-1: the adopted OAuth app secret
