"""The enrolment core (UI split P1): everything that makes a phone's one-time QR, behind one function.

`create_enrollment` is what the options dialog's steps (enrollment_flow.PorticoOptionsFlow) call, and what a later
panel command will call. It is NOT a pure move of the dialog's code: it is the dialog's logic, in the dialog's order,
with a typed result in place of form results. The order is load-bearing and is kept exactly:

  1. the name (stripped, 1 to 24 characters);
  2. `ha_dial` parsed, then the first URL check (`no_ha_url`, `no_https_url`);
  3. the re-enrol confirm check (per phone entry id), BEFORE any key is minted or spent;
  4. the Tailscale answer (backfilling `tailscale_mode: auto` on an entry from before the option existed);
  5. the pending cap (new in P1, shared with the dialog), BEFORE any key is minted and any user is created;
  6. the Tailscale mint for `auto`, or the key an `approve` approval already produced (`approved_authkey`);
  7. the user (created or reused), the payload, the QR, the remembered `ha_dial` write-back, the phone entry;
  8. the code goes live, the persistent notice is raised.

Every failure after the user exists removes the user again (steps 7); a failure before it leaves nothing behind.

The helpers (`_select_ha_url`, `_auto_ha_dial`, `_async_mint_tailscale_authkey`, `_fcm_block`, `_build_qr_data_uri`,
`_make_qr`...) stay in enrollment_flow and are called THROUGH ITS MODULE NAMESPACE (`enrollment_flow.<name>`), never
imported by name: the existing tests replace them there (monkeypatch on `enrollment_flow`), and a name bound at import
time would walk past every one of those patches.

One `asyncio.Lock` per hass serialises whole enrolments, from the confirm check to the point the phone entry exists, so
two callers with one name create one user and the cap cannot be passed by two at once. Enrolling is rare and
admin-only; waiting out a Tailscale mint (30 s at most) is the cost.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import math
import secrets
import struct
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from typing import Any

from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import enroll_redeem, enrollment_flow, phone_entry
from .ha_dial import parse_ha_dial
from .phone_entry import (
    STATE_COMPROMISED,
    STATE_ENROLLED,
    STATE_PENDING,
    TOKEN_KIND_LLAT,
    PhoneRecord,
)

_LOGGER = logging.getLogger(__name__)

NAME_MAX = 24

# At most this many `pending` phones with a live code at once (a re-draw for the same phone is not a second one).
PENDING_CAP = 5

# Notes on a `Created` result, as codes (the dialog and the panel word them themselves).
NOTE_REPLACES_EARLIER = "replaces_earlier_qr"      # a pending / flagged phone drawn again: its earlier QR is dead
NOTE_REENROLL_LONG_LIVED = "reenroll_long_lived"   # a phone enrolled before one-time codes: old token works until used
NOTE_FCM_SKIPPED = "fcm_skipped"                   # the push ring notification Firebase values were unusable and left out
NOTE_MANUAL_TAILSCALE = "manual_tailscale"         # `manual` mode: the QR carries no Tailscale key

# Error codes (`Error.code`).
ERR_BAD_NAME = "bad_name"
ERR_INVALID_HA_DIAL = "invalid_ha_dial"
ERR_NO_HA_URL = "no_ha_url"
ERR_NO_HTTPS_URL = "no_https_url"
ERR_PENDING_CAP = "pending_cap"
ERR_MINT_FAILED = "tailscale_mint_failed"
ERR_NO_PUSH_ENTRY = "no_push_entry"
ERR_UNKNOWN = "unknown"

_DATA_LOCK = "portico_enroll_core_lock"


# ---- the typed result ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Created:
    """The QR was made and its code is live. `qr_png` is the PNG (not a data URI); it and nothing else here is
    secret-bearing (the QR decodes to the one-time code and, in `auto` mode, a pre-authorised Tailscale key)."""

    entry_id: str                      # the phone's config entry
    ha_user_id: str
    expires_in: int                    # seconds the code has left, at the reply
    expires_at: datetime               # the same moment, UTC
    qr_png: bytes = dc_field(repr=False)
    qr_text: str = dc_field(repr=False)   # exactly the text the QR encodes (secret-bearing like the PNG): the paste field's twin
    modules: int                       # QR modules per side, without the quiet zone (0: not a PNG this code can read)
    mode: str                          # the Tailscale mode the QR was made under
    notes: tuple[str, ...]             # NOTE_* codes
    dial_target: str | None            # what the phone dials over the tailnet: the `ha_dial` in the QR, else ha_url's
    created: bool                      # True: a new phone entry; False: an existing one drawn again


@dataclass(frozen=True)
class NeedsTailscaleSetup:
    """The Tailscale question has no complete answer: finish it in the dialog (`mode` is the unfinished one or None)."""

    mode: str | None


@dataclass(frozen=True)
class NeedsApproval:
    """`approve` mode and no key was supplied: the admin must approve this phone on Tailscale's consent screen."""


@dataclass(frozen=True)
class NeedsConfirmReenroll:
    """The name belongs to a phone enrolled with a long-lived token: ask before replacing it. Call again with
    `confirmed_entry_id=entry_id` once the admin agreed."""

    entry_id: str
    name: str


@dataclass(frozen=True)
class Error:
    """A refusal. `code` is a strings.json error key (or `no_push_entry` / `unknown`); `field` names the form field it
    belongs to, when one does. `exception` is set on `unknown` (the exception that was caught, after rollback)."""

    code: str
    field: str | None = None
    exception: BaseException | None = dc_field(default=None, repr=False, compare=False)


EnrollmentResult = Created | NeedsTailscaleSetup | NeedsApproval | NeedsConfirmReenroll | Error


# ---- the entry point -----------------------------------------------------------------------------------------------


async def create_enrollment(
    hass: HomeAssistant,
    push_entry: ConfigEntry | None,
    *,
    name: str,
    allow_http: bool,
    ha_dial_raw: str | None,
    confirmed_entry_id: str | None,
    approved_authkey: str | None = None,
    qr_box_size: int | None = None,
) -> EnrollmentResult:
    """Draw a one-time QR for the phone `name`. See the module docstring for the order. `qr_box_size` is pixels per
    QR module in the PNG (None: the dialog's `enrollment_flow.QR_BOX_SIZE`)."""
    clean = name.strip() if isinstance(name, str) else ""
    if not 1 <= len(clean) <= NAME_MAX:
        return Error(ERR_BAD_NAME, "phone_name")
    if push_entry is None:
        return Error(ERR_NO_PUSH_ENTRY)
    typed = (ha_dial_raw or "").strip()
    ha_dial_typed: str | None = None
    if typed:
        ha_dial_typed = parse_ha_dial(typed)
        if ha_dial_typed is None:
            # Before any key, user or token: the app would refuse the whole QR (DEV-557).
            return Error(ERR_INVALID_HA_DIAL, "ha_dial")
    ha_url, error = enrollment_flow._select_ha_url(hass, allow_http=allow_http)
    if error is not None:
        return Error(error)
    async with _lock(hass):
        return await _enroll_locked(
            hass, push_entry, clean, ha_url, ha_dial_typed, confirmed_entry_id, approved_authkey, qr_box_size)


def _lock(hass: HomeAssistant) -> asyncio.Lock:
    lock = hass.data.get(_DATA_LOCK)
    if lock is None:
        lock = hass.data[_DATA_LOCK] = asyncio.Lock()
    return lock


async def _enroll_locked(
    hass: HomeAssistant,
    entry: ConfigEntry,
    name: str,
    ha_url: str,
    ha_dial_typed: str | None,
    confirmed_entry_id: str | None,
    approved_authkey: str | None,
    qr_box_size: int | None,
) -> EnrollmentResult:
    # The re-enrol confirm: before the Tailscale approval or any key is spent on it.
    reuse = await _async_reusable_phone(hass, name)
    if _needs_confirm(reuse, confirmed_entry_id):
        return NeedsConfirmReenroll(reuse.entry_id, PhoneRecord.from_data(reuse.data).name)

    if "tailscale_mode" not in entry.data:
        # An entry from before the option existed already has `tailscale_api_key` (the only mode there used to be):
        # backfill `tailscale_mode` rather than re-asking a question that already has an answer on disk.
        if "tailscale_api_key" not in entry.data:
            return NeedsTailscaleSetup(None)
        hass.config_entries.async_update_entry(entry, data={**entry.data, "tailscale_mode": "auto"})
    if not await enrollment_flow._async_tailscale_answer_complete(hass, entry):
        return NeedsTailscaleSetup(entry.data.get("tailscale_mode"))
    mode = entry.data["tailscale_mode"]

    # The cap, before a key is minted and before a user exists. The phone this name would reuse is not counted:
    # drawing it again does not make a sixth. A phone already enrolled (a long-lived token being replaced) never
    # becomes `pending`, so it adds none and is not held back by the cap.
    redraw_of_enrolled = reuse is not None and PhoneRecord.from_data(reuse.data).state == STATE_ENROLLED
    if not redraw_of_enrolled \
            and _live_pending(hass, exclude=reuse.entry_id if reuse is not None else None) >= PENDING_CAP:
        return Error(ERR_PENDING_CAP)

    # Before the user: HA's network helper is the last thing that can be slow. Never a value portico-app would
    # refuse (that refuses the whole QR), never a public address.
    ha_dial = ha_dial_typed or await enrollment_flow._auto_ha_dial(hass, ha_url)
    tailscale_authkey: str | None = None
    if mode == "auto":
        try:
            tailscale_authkey = await enrollment_flow._async_mint_tailscale_authkey(
                hass,
                api_key=entry.data["tailscale_api_key"].strip(),
                tailnet=entry.data["tailscale_tailnet"].strip(),
                tag=enrollment_flow._stripped(entry.data.get("tailscale_tag")) or enrollment_flow._LEGACY_TAILSCALE_TAG,
            )
        except enrollment_flow._MINT_ERRORS as err:
            # The exception class and HTTP status only: never the key, never the response body.
            _LOGGER.warning(
                "Portico: could not mint a Tailscale key for entry %s (%s, HTTP status %s)",
                entry.entry_id,
                type(err).__name__,
                getattr(err, "status", None),
            )
            return Error(ERR_MINT_FAILED)
    elif mode == "approve":
        if approved_authkey is None:
            return NeedsApproval()
        tailscale_authkey = approved_authkey

    # "Show a new QR" / enrolling a phone again (DEV-544): a name that matches a phone still waiting for its code, or
    # one enrolled before the one-time code existed, reuses that phone's user and enroll_id. Looked at again: the phone
    # may have changed while the key was minted or approved.
    reuse = await _async_reusable_phone(hass, name)
    if _needs_confirm(reuse, confirmed_entry_id):
        return NeedsConfirmReenroll(reuse.entry_id, PhoneRecord.from_data(reuse.data).name)

    created_user = None
    try:
        if reuse is None:
            created_user = user = await hass.auth.async_create_user(name=f"Portico — {name}", group_ids=[GROUP_ID_USER])
            enroll_id = secrets.token_urlsafe(32)
        else:
            user = await hass.auth.async_get_user(reuse.data["ha_user_id"])
            enroll_id = reuse.data["enroll_id"]
        code = enroll_redeem.new_code()
        payload: dict[str, Any] = {
            "portico": enrollment_flow.PAYLOAD_FORMAT,
            "ha_url": ha_url,
            # The one-time code (enroll_redeem.py): redeemed over HTTP for a refresh token. Never a token.
            "code": code,
            "phone_name": name,
            # Offline registration (reg_string_spec.md): a random id (the only credential behind offline
            # registration; diagnostics redact it). An older app ignores the key.
            "enroll_id": enroll_id,
        }
        if tailscale_authkey is not None:
            payload["tailscale_authkey"] = tailscale_authkey
        if ha_dial is not None and parse_ha_dial(ha_dial) == ha_dial:
            payload["ha_dial"] = ha_dial
        # `manual` mode: no key minted, no "tailscale_authkey" key at all in the payload.
        fcm, fcm_skipped = enrollment_flow._fcm_block(hass, entry.domain)
        if fcm is not None:
            payload["fcm"] = fcm
        # CPU-bound (QR matrix + PNG encode): off the event loop. Always through the flow module's name, which the
        # tests replace; the box size is passed only when asked for, so a patched one-argument builder still fits.
        if qr_box_size is None:
            qr_data_uri = await hass.async_add_executor_job(enrollment_flow._build_qr_data_uri, payload)
        else:
            qr_data_uri = await hass.async_add_executor_job(enrollment_flow._build_qr_data_uri, payload, qr_box_size)
        qr_png = base64.b64decode(qr_data_uri.partition(",")[2], validate=True)

        data = dict(entry.data)
        # A typed address is the next enrollment's suggestion; a blank field (automatic) forgets it.
        data.pop("ha_dial", None)
        if ha_dial_typed is not None:
            data["ha_dial"] = ha_dial_typed
        hass.config_entries.async_update_entry(entry, data=data)
        if reuse is None:
            # The phone is its own entry (P-4): identity facts only, no secret. `pending` until the code is redeemed.
            # Last of the things that can fail, so a failure above leaves nothing behind but the user removed below.
            phone = await phone_entry.async_create_phone_entry(
                hass,
                phone_entry.new_record(ha_user_id=user.id, name=name, enroll_id=enroll_id, tailscale_mode=mode),
            )
            phone_entry_id = phone.entry_id
        else:
            _renew_pending_phone(hass, reuse, mode)
            phone_entry_id = reuse.entry_id
    except Exception as err:  # noqa: BLE001 - every failure is a typed result, after the rollback
        await _rollback(hass, created_user)
        _LOGGER.warning("Portico: enrolling a phone for entry %s failed (%s)", entry.entry_id, type(err).__name__)
        return Error(ERR_UNKNOWN, exception=err)
    except BaseException:
        await _rollback(hass, created_user)
        raise

    # Nothing after this can fail: the code goes live (a code the phone had before is dead from here).
    registry = enroll_redeem.get_registry(hass)
    expires_at = registry.register(enroll_id, code, user.id, name)
    left = registry.seconds_left(enroll_id)
    expires_in = math.ceil(left) if left is not None else int(enroll_redeem.CODE_LIFETIME.total_seconds())
    _raise_notice(hass, phone_entry_id, name, expires_at)
    notes: list[str] = []
    if reuse is not None:
        enrolled = PhoneRecord.from_data(reuse.data).state == STATE_ENROLLED
        notes.append(NOTE_REENROLL_LONG_LIVED if enrolled else NOTE_REPLACES_EARLIER)
    if fcm_skipped:
        notes.append(NOTE_FCM_SKIPPED)
    if mode == "manual":
        notes.append(NOTE_MANUAL_TAILSCALE)
    _LOGGER.info(
        "Portico: %s phone %r (HA user %s) for entry %s",
        "drew a new enrollment QR for" if reuse is not None else "enrolling",
        name,
        user.id,
        entry.entry_id,
    )
    return Created(
        entry_id=phone_entry_id,
        ha_user_id=user.id,
        expires_in=expires_in,
        expires_at=expires_at,
        qr_png=qr_png,
        qr_text=enrollment_flow._qr_text(payload),
        modules=_modules(qr_png, qr_box_size),
        mode=mode,
        notes=tuple(notes),
        dial_target=payload.get("ha_dial") or enrollment_flow._url_target(ha_url),
        created=reuse is None,
    )


# ---- helpers -------------------------------------------------------------------------------------------------------


def _needs_confirm(reuse: ConfigEntry | None, confirmed_entry_id: str | None) -> bool:
    return (
        reuse is not None
        and reuse.entry_id != confirmed_entry_id
        and PhoneRecord.from_data(reuse.data).state == STATE_ENROLLED
    )


async def _async_reusable_phone(hass: HomeAssistant, name: str) -> ConfigEntry | None:
    """The phone entry a new QR for `name` is for, or None for a new phone: one still `pending` or `compromised` (a
    pending phone first), else one enrolled before DEV-544 that holds a ten-year token. It needs an `enroll_id` (a row
    from before those has none and is left alone) and a live HA user."""
    candidates = []
    for found in phone_entry.phone_entries(hass):
        if found.data.get("name") != name or not found.data.get("enroll_id"):
            continue
        record = PhoneRecord.from_data(found.data)
        if record.state in (STATE_PENDING, STATE_COMPROMISED):
            candidates.append((0 if record.state == STATE_PENDING else 1, found))
        elif record.state == STATE_ENROLLED and record.token_kind == TOKEN_KIND_LLAT:
            candidates.append((2, found))
    for _rank, found in sorted(candidates, key=lambda c: c[0]):
        user = await hass.auth.async_get_user(found.data["ha_user_id"])
        if user is not None and user.is_active:
            return found
    return None


def _renew_pending_phone(hass: HomeAssistant, entry: ConfigEntry, tailscale_mode: str) -> None:
    """A phone drawn again: one waiting for its code (or flagged) is `pending` from now; one that holds a ten-year
    token is left as it is, and works until its new code is redeemed."""
    record = PhoneRecord.from_data(entry.data)
    if record.state == STATE_ENROLLED:
        return
    phone_entry.async_update_phone(
        hass, entry, state=STATE_PENDING, pending_since=dt_util.utcnow().isoformat(), tailscale_mode=tailscale_mode,
    )


def _live_pending(hass: HomeAssistant, *, exclude: str | None) -> int:
    """How many phone entries are `pending` with a code that still works (the phone `exclude` aside)."""
    registry = enroll_redeem.get_registry(hass)
    count = 0
    for found in phone_entry.phone_entries(hass):
        if found.entry_id == exclude:
            continue
        record = PhoneRecord.from_data(found.data)
        if record.state == STATE_PENDING and record.enroll_id and registry.seconds_left(record.enroll_id) is not None:
            count += 1
    return count


async def _rollback(hass: HomeAssistant, user) -> None:
    """Remove the user this enrolment made (its refresh tokens go with it). Never raises: the failure being handled
    is the one to report."""
    if user is None:
        return
    try:
        await hass.auth.async_remove_user(user)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Portico: could not remove the user of a failed enrollment")


def _modules(png: bytes, box_size: int | None) -> int:
    """QR modules per side, read from the PNG's width (width = (modules + 2 * 4-module border) * box size)."""
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
        return 0
    (width,) = struct.unpack(">I", png[16:20])
    box = box_size or enrollment_flow.QR_BOX_SIZE
    return max(width // box - 8, 0)


def _raise_notice(hass: HomeAssistant, phone_entry_id: str, name: str, expires_at: datetime) -> None:
    """The persistent notice that a QR is waiting. It names the phone and when the code stops working; NEVER the code
    (nor a key, nor the QR): a notice is shown to every admin and kept in HA's state. Dismissed when the phone enrols
    (enroll_redeem) or its entry is removed (phone_entry.async_revoke_phone)."""
    try:
        from homeassistant.components import persistent_notification

        persistent_notification.async_create(
            hass,
            f"A QR code for the phone \"{name}\" is waiting to be scanned. It works once, until "
            f"{dt_util.as_local(expires_at).strftime('%H:%M')}. It goes away when the phone enrols; removing the "
            f"phone cancels it.",
            title="Portico phone enrollment",
            notification_id=phone_entry.enroll_notification_id(phone_entry_id),
        )
    except Exception:  # noqa: BLE001 - a notice is a courtesy; the QR is made
        _LOGGER.exception("Portico: could not raise the enrollment notice")
