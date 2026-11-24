"""P-4 of the Portico split: each enrolled phone is one config entry of the UI part (ENROLL-01).

An entry is created through the config flow's `phone` source (`config_flow.async_step_phone`), from the enrollment
options flow of the settings (push) entry, or from `async_adopt_phones` for a phone enrolled before this version. Its
data holds only identity, no token or key (`PhoneRecord`; its `enroll_id` is not a token but is redacted from diagnostics); its options hold per-phone settings (today: the doors it may
open). No token, Tailscale key or registration value is ever in an entry.

Removing the entry is revoking the phone (`async_revoke_phone`): the phone's HA tokens, its push registration, its
content override, a code still waiting to be redeemed. The Tailscale node is not touched (DEV-544 decision 6: no automatic
delete): a notice names it and says where to delete it.

`phone_rows` is the one reader every consumer goes through (`push.registration._phones`): phone entries first, then
the legacy `enrolled_phones` rows still on a Portico entry (an unadopted phone is never lost).
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, fields
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .door.const import is_door_entry
from .push import const

_LOGGER = logging.getLogger(__name__)

DOMAIN = "portico"

# The config-flow source that creates a phone entry (SPK-6: HA accepts any source string; the flow manager calls
# `async_step_<source>`).
SOURCE_PHONE = "phone"
ENTRY_TYPE_PHONE = "phone"
UNIQUE_ID_PREFIX = "phone:"

# Entry options.
CONF_DOORS = "doors"      # None/absent: every door; a list: only these `Door.id`s
# M7 3a (HC-2): "Receive house calls in Portico". Absent or false = off, the default, so installing Portico never doubles a
# house ring on a phone that also has the old app. Nothing in 3a offers a switch for it (no UI); an installer's options
# edit keeps it (`PhoneOptionsFlow` writes `{**options, doors: ...}`).
CONF_HOUSE_RECEIVE = "house_receive"
# DEV-807 panel mode: a panel enrols as a phone and owns its pages like one. `panel: true` alone marks the entry.
# A legacy `panel_screen` option (the screen it used before it owned its pages) may still sit on an old entry: nothing reads
# or writes it, and it is simply ignored. Every writer of a phone entry's
# options spreads the existing options (`{**entry.options, ...}`), so these survive them; a legacy row has no options.
CONF_PANEL = "panel"

# ENROLL-04: what the phone holds. `llat` is the ten-year long-lived token a phone enrolled before DEV-544 holds (no
# new enrollment makes one); `refresh` is DEV-544's HA refresh token (24 h access tokens), issued when the phone
# redeems its one-time code (enroll_redeem.py).
TOKEN_KIND_LLAT = "llat"
TOKEN_KIND_REFRESH = "refresh"
TOKEN_KINDS = (TOKEN_KIND_LLAT, TOKEN_KIND_REFRESH)
REFRESH_STATE_NONE = "none"           # a long-lived token has no refresh state
REFRESH_STATE_PENDING = "pending"     # a refresh phone whose code is not redeemed yet: it holds no token at all
REFRESH_STATE_ACTIVE = "active"       # a refresh token has been issued to the phone

# DEV-544: where the phone is in its enrollment. An absent value reads as `enrolled`, so every row and entry from
# before DEV-544 stays valid. `pending`: the QR was drawn and nobody redeemed it (the user exists, holds no token).
# `compromised`: the code was presented twice by different holders (the issued token was revoked, a repair issue
# raised); it stays so until a new QR is drawn for it.
STATE_ENROLLED = "enrolled"
STATE_PENDING = "pending"
STATE_COMPROMISED = "compromised"
STATES = (STATE_ENROLLED, STATE_PENDING, STATE_COMPROMISED)

NAME_PREFIX = "Portico — "            # the HA user enrollment creates is named "Portico — <phone name>"


@dataclass(frozen=True, slots=True)
class PhoneRecord:
    """A phone entry's data. No token, key or registration value; `enroll_id` is redacted in diagnostics."""

    ha_user_id: str
    name: str
    enroll_id: str | None = None          # None only for a row enrolled before enroll ids existed
    enrolled_at: str = ""
    token_kind: str = TOKEN_KIND_LLAT
    token_expires_at: str | None = None    # when the access token stops working; None: not known
    refresh_state: str = REFRESH_STATE_NONE
    tailscale_mode: str | None = None      # the mode the phone was enrolled under
    tailscale_node: str | None = None      # the node's name once known (DEV-544 decision 6); None today
    state: str = STATE_ENROLLED            # DEV-544: enrolled | pending | compromised (see above)
    pending_since: str | None = None       # when the current QR was drawn, while `pending` (the 24 h sweep's clock)

    @property
    def key(self) -> str:
        """The phone's stable identity (ENDPOINT-02): its `enroll_id`, or for a row from before enroll ids its HA user
        id (the same rule as `endpoints._phone_endpoint`)."""
        return self.enroll_id or self.ha_user_id

    def to_data(self) -> dict[str, Any]:
        return {const.ENTRY_TYPE: ENTRY_TYPE_PHONE, **asdict(self)}

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> PhoneRecord:
        """From entry data or a legacy `enrolled_phones` row (ValueError when it names no HA user)."""
        user_id = data.get("ha_user_id")
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("a phone needs an ha_user_id")
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        values["name"] = str(data.get("name") or "phone")
        values["enrolled_at"] = str(data.get("enrolled_at") or "")
        if values.get("enroll_id") is not None and not isinstance(values["enroll_id"], str):
            values["enroll_id"] = None
        if values.get("token_kind") not in TOKEN_KINDS:
            values["token_kind"] = TOKEN_KIND_LLAT
        if values.get("state") not in STATES:
            values["state"] = STATE_ENROLLED
        return cls(**values)


def is_phone_entry(entry: ConfigEntry) -> bool:
    return entry.data.get(const.ENTRY_TYPE) == ENTRY_TYPE_PHONE


def phone_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return [e for e in hass.config_entries.async_entries(DOMAIN) if is_phone_entry(e)]


def phone_device_identifiers(entry: ConfigEntry) -> set[tuple[str, str]]:
    """The phone's HA device (one per entry, ENROLL-01). Keyed by the entry id: nothing secret in the registry."""
    return {(DOMAIN, f"phone_{entry.entry_id}")}


def phone_entry_for_user(hass: HomeAssistant, user_id: str) -> ConfigEntry | None:
    for entry in phone_entries(hass):
        if entry.data.get("ha_user_id") == user_id:
            return entry
    return None


def phone_dict(entry: ConfigEntry) -> dict[str, Any]:
    """A phone entry as the consumers read a phone: the data's keys plus `doors` from the options."""
    return {**entry.data, CONF_DOORS: entry.options.get(CONF_DOORS)}


def phone_rows(hass: HomeAssistant):
    """(entry, phone) for every enrolled phone: the phone entries, then the legacy `enrolled_phones` rows on any
    other Portico entry (the settings entry before it adopts them; an old-format door entry), live or not. A door entry
    (`entry_type: door`) holds no phones and is skipped. A phone in two
    places (same HA user, or same enroll id) is yielded once, the phone entry first."""
    entries = hass.config_entries.async_entries(DOMAIN)
    seen_users: set[str] = set()
    seen_keys: set[str] = set()
    for entry in entries:
        if not is_phone_entry(entry):
            continue
        phone = phone_dict(entry)
        user_id = phone.get("ha_user_id")
        if user_id:
            seen_users.add(user_id)
        if phone.get("enroll_id"):
            seen_keys.add(phone["enroll_id"])
        yield entry, phone
    # Settings entry last, so a device entry's row wins the (never seen) tie, as before P-4.
    legacy = sorted((e for e in entries if not is_phone_entry(e) and not is_door_entry(e)),
                    key=lambda e: e.data.get(const.ENTRY_TYPE) == const.ENTRY_TYPE_PUSH)
    for entry in legacy:
        for phone in entry.data.get("enrolled_phones", []):
            user_id = phone.get("ha_user_id")
            enroll_id = phone.get("enroll_id")
            if user_id:
                if user_id in seen_users:
                    continue
                seen_users.add(user_id)
            if enroll_id:
                if enroll_id in seen_keys:
                    continue
                seen_keys.add(enroll_id)
            yield entry, phone


def phone_house_receive(hass: HomeAssistant, user_id: str | None) -> bool:
    """Whether the phone of this HA user receives house calls in Portico (HC-2). A legacy `enrolled_phones` row has no
    options and so does not; only a phone entry whose option is exactly true does."""
    if not user_id:
        return False
    entry = phone_entry_for_user(hass, user_id)
    return entry is not None and entry.options.get(CONF_HOUSE_RECEIVE) is True


def async_set_house_receive(hass: HomeAssistant, entry: ConfigEntry, receive: bool) -> None:
    """Switch a phone's house-call receiving on or off (its entry's options). A house call ringing for a phone that
    just lost the switch ends (the registry reconciles). Used by later PRs' UI; 3a calls it from tests only."""
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_HOUSE_RECEIVE: bool(receive)})
    _house_reconcile(hass)


def _house_reconcile(hass: HomeAssistant) -> None:
    """Tell the house-call registry, when there is one, that the roster changed. A no-op while house calls are off."""
    from .endpoints import announce_panels
    from .house import get_house

    house = get_house(hass)
    if house is not None:
        house.reconcile()
    announce_panels(hass)       # DEV-807: a panel's `house_calls` follows its `house_receive`; its app refetches


def may_open(phone: dict[str, Any], door_id: str) -> bool:
    """Whether this phone is enrolled for a door (R4-04): every door unless the phone's `doors` option lists some."""
    doors = phone.get(CONF_DOORS)
    return doors is None or door_id in doors


def new_record(*, ha_user_id: str, name: str, enroll_id: str, tailscale_mode: str | None) -> PhoneRecord:
    """A phone whose QR was just drawn (DEV-544): `pending`, holding no token. It becomes `enrolled` when the phone
    redeems its code (`enroll_redeem`)."""
    now = dt_util.utcnow().isoformat()
    return PhoneRecord(
        ha_user_id=ha_user_id, name=name, enroll_id=enroll_id, enrolled_at=now,
        token_kind=TOKEN_KIND_REFRESH, token_expires_at=None, refresh_state=REFRESH_STATE_PENDING,
        tailscale_mode=tailscale_mode, state=STATE_PENDING, pending_since=now,
    )


def async_update_phone(hass: HomeAssistant, entry: ConfigEntry, **changes: Any) -> None:
    """Write `changes` into the phone entry's data (known `PhoneRecord` fields only; nothing secret goes here)."""
    known = {f.name for f in fields(PhoneRecord)}
    unknown = set(changes) - known
    if unknown:
        raise ValueError(f"not phone fields: {sorted(unknown)}")
    hass.config_entries.async_update_entry(entry, data={**entry.data, **changes})


async def async_create_phone_entry(hass: HomeAssistant, record: PhoneRecord) -> ConfigEntry:
    """Create the phone's entry through the config flow's `phone` source. Raises RuntimeError when the flow does
    not create one (the caller removes what it made)."""
    from homeassistant.data_entry_flow import FlowResultType

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_PHONE}, data=record.to_data())
    if result["type"] is not FlowResultType.CREATE_ENTRY:
        raise RuntimeError(f"the phone entry was not created ({result.get('reason') or result['type']})")
    return result["result"]


def _has_entry(hass: HomeAssistant, key: str) -> bool:
    unique_id = f"{UNIQUE_ID_PREFIX}{key}"
    return any(e.unique_id == unique_id for e in phone_entries(hass))


async def async_adopt_phones(hass: HomeAssistant, host: ConfigEntry) -> list[str]:
    """Turn the `enrolled_phones` rows on the settings entry `host` into phone entries, once each, then drop the rows
    that now have an entry. Returns the adopted phones' names (for the log; never a user id or enroll id).

    Idempotent and never loses a phone: a row whose entry exists (a previous, interrupted run) is just dropped; a row
    that cannot become an entry (no HA user, or the flow refuses) stays where it is and is still read as a phone; a
    second run finds nothing to do. The rows go only after their entry is created, so a crash between the two leaves
    both, and `phone_rows` yields the phone once. Rows on an old door entry are not adopted (the migration deletes
    those entries; MIGR-01)."""
    rows = list(host.data.get("enrolled_phones", []))
    if not rows:
        return []
    adopted: list[str] = []
    kept: list[dict[str, Any]] = []
    for row in rows:
        try:
            record = PhoneRecord.from_data(row)
        except ValueError:
            _LOGGER.warning("portico: an enrolled-phone row without an HA user stays on the entry, unadopted")
            kept.append(row)
            continue
        if not _has_entry(hass, record.key):
            try:
                await async_create_phone_entry(hass, record)
            except Exception:  # noqa: BLE001 - one bad row must not cost the others
                _LOGGER.exception("portico: could not turn the enrolled phone %r into an entry; it stays a row", record.name)
                kept.append(row)
                continue
            adopted.append(record.name)
    live = hass.config_entries.async_get_entry(host.entry_id)
    if live is not None and len(kept) != len(rows):
        data = {k: v for k, v in live.data.items() if k != "enrolled_phones"}
        if kept:
            data["enrolled_phones"] = kept
        hass.config_entries.async_update_entry(live, data=data)
    if adopted:
        _LOGGER.info("portico: %d enrolled phone(s) became phone entries", len(adopted))
    return adopted


def drop_legacy_row(hass: HomeAssistant, host: ConfigEntry, phone: dict[str, Any]) -> None:
    """Take one legacy `enrolled_phones` row off the entry `host` that holds it (UI split P3, revoke from the Phones tab).
    The row is matched by identity first, then by equality; the key goes when the last row does. Nothing else is touched:
    the caller revokes what the row held."""
    rows = list(host.data.get("enrolled_phones", []))
    index = next((i for i, row in enumerate(rows) if row is phone), None)
    if index is None:
        index = next((i for i, row in enumerate(rows) if row == phone), None)
    if index is None:
        return
    del rows[index]
    data = {k: v for k, v in host.data.items() if k != "enrolled_phones"}
    if rows:
        data["enrolled_phones"] = rows
    hass.config_entries.async_update_entry(host, data=data)


# ---- revocation (ENROLL-05, PUSH-04) -------------------------------------------------------------------------------

async def async_revoke_phone(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Everything a removed phone entry takes with it. Each part is tried on its own: a failure is logged and the
    next part still runs (the entry is already gone from HA's list; there is no second chance).

    - its HA tokens: a user Portico made ("Portico — <name>", not an admin, not the owner) is removed with all its
      tokens; any other user keeps its account and loses only the tokens enrollment made (`portico-...`);
    - its push registrations, by enroll id (and, for rows older than that field, by HA user);
    - its content override;
    - a one-time enrollment code still waiting to be redeemed (DEV-544), so a removed phone's QR is dead at once,
      and the "a QR is waiting" notice that enrolment raised for it;
    - a notice naming the Tailscale node, which is not removed (DEV-544 decision 6)."""
    record = PhoneRecord.from_data(entry.data)
    # A panel's pages live in its own device row, which `async_revoke_record` removes with the phone's override: no screen
    # goes with it any more (own-pages design section 5 item 4). A former panel screen that was never deleted stays, as an
    # ordinary screen, until the editor deletes it.
    await async_revoke_record(hass, record, entry.entry_id)


async def async_revoke_record(hass: HomeAssistant, record: PhoneRecord, entry_id: str | None) -> None:
    """The body of `async_revoke_phone`, for a record that may not have an entry of its own: a legacy `enrolled_phones`
    row revoked from the Phones tab (UI split P3) goes through exactly the same steps, so the HA user guard
    (`_made_by_portico`) is one rule for both. `entry_id` names the persistent notice to take down, if any."""
    _guard_sync(lambda: _drop_enroll_code(hass, record), "dropping the phone's pending enrollment code")
    if entry_id:
        _guard_sync(lambda: dismiss_enroll_notification(hass, entry_id), "dismissing the phone's enrollment notice")
    _guard_sync(lambda: _house_reconcile(hass), "ending the phone's house calls")
    await _guard(_revoke_tokens(hass, record), "revoking the phone's Home Assistant tokens")
    await _guard(_remove_registrations(hass, record), "removing the phone's push registration")
    await _guard(_remove_override(hass, record), "removing the phone's content override")
    _guard_sync(lambda: _notify_tailscale_node(hass, record), "raising the Tailscale notice")


def enroll_notification_id(entry_id: str) -> str:
    """The id of the persistent notice that a phone's QR is waiting to be scanned (enrollment_core raises it; it
    carries no code)."""
    return f"portico_enroll_{entry_id}"


def dismiss_enroll_notification(hass: HomeAssistant, entry_id: str) -> None:
    """Take that notice down (the phone enrolled, or its entry was removed). Harmless when there is none."""
    from homeassistant.components import persistent_notification

    persistent_notification.async_dismiss(hass, enroll_notification_id(entry_id))


def _drop_enroll_code(hass: HomeAssistant, record: PhoneRecord) -> None:
    from .enroll_redeem import get_registry

    if record.enroll_id:
        get_registry(hass).discard(record.enroll_id)


async def _guard(coro, what: str) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001 - see async_revoke_phone
        _LOGGER.exception("portico: %s failed", what)


def _guard_sync(call, what: str) -> None:
    try:
        call()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("portico: %s failed", what)


def _made_by_portico(user) -> bool:
    return bool(user.name and user.name.startswith(NAME_PREFIX)) and not user.is_admin and not user.is_owner \
        and not user.system_generated


async def _revoke_tokens(hass: HomeAssistant, record: PhoneRecord) -> None:
    user = await hass.auth.async_get_user(record.ha_user_id)
    if user is None:
        return                                        # deleted in Settings > People: its tokens went with it
    if _made_by_portico(user):
        for token in list(user.refresh_tokens.values()):
            hass.auth.async_remove_refresh_token(token)
        await hass.auth.async_remove_user(user)
        return
    for token in list(user.refresh_tokens.values()):
        if (token.client_name or "").startswith("portico-"):
            hass.auth.async_remove_refresh_token(token)
    _LOGGER.warning("portico: kept the Home Assistant user of the removed phone %r (not a user Portico made); "
                    "only the tokens enrollment made were revoked", record.name)


async def _remove_registrations(hass: HomeAssistant, record: PhoneRecord) -> None:
    from .push import get_manager
    from .push.registry import PushRegistry

    manager = get_manager(hass)
    if manager is not None:
        await manager.registry.async_remove_phone(record.enroll_id, record.ha_user_id)
        return
    # The push ring notification is not loaded: nothing else holds the store, so edit it directly.
    registry = PushRegistry(hass)
    await registry.async_load()
    await registry.async_remove_phone(record.enroll_id, record.ha_user_id)


async def _remove_override(hass: HomeAssistant, record: PhoneRecord) -> None:
    from .content import get_content_store

    await get_content_store(hass).async_remove(record.ha_user_id, record.key)


def _notify_tailscale_node(hass: HomeAssistant, record: PhoneRecord) -> None:
    from homeassistant.components import persistent_notification

    if record.tailscale_node:
        node = f"Its Tailscale node is {record.tailscale_node}."
    elif record.tailscale_mode == "manual":
        node = "It joined Tailscale on its own (manual mode), so Portico never knew its node."
    else:
        node = f"Its Tailscale node was created when the phone joined; it is named after the phone, \"{record.name}\"."
    persistent_notification.async_create(
        hass,
        f"The phone \"{record.name}\" was removed: its Home Assistant access and its push ring notification registration are "
        f"revoked. {node} Portico does not delete the node: remove it in the Tailscale admin console (Machines).",
        title="Portico phone removed",
        notification_id=f"portico_phone_removed_{record.ha_user_id}",
    )
