"""PushRegistry: one row per portico-app installation (sender design §3.2).

Domain-level, in a 0600 private Store (`.storage/portico.push_registry`),
never in a config entry. Rows are upserted on (ha_user_id, installation_id),
so a reinstall or a token/FID rotation replaces the row instead of adding
one. A row's `registration` value never leaves this module except into the
FCM request body: not into states, logs, diagnostics or `push/status`.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import const

_LOGGER = logging.getLogger(__name__)

STALE_AFTER = timedelta(days=60)
EXPIRE_AFTER = timedelta(days=270)  # Google's own expiry for an inactive registration
# HA-L8: at most this many rows per HA user. Every row is a ring target (notifier), so without a cap a
# user's reinstalls (each mints a new installation_id) pile up phantom targets until the 270-day age-out.
MAX_ROWS_PER_USER = 3
_SAVE_DELAY = 2

STATE_OK = "ok"
STATE_UNREGISTERED = "unregistered"
STATE_PROJECT_MISMATCH = "project_mismatch"
STATE_NEVER_SENT = "never_sent"

# What `portico/push/status` and the sensor may show about a row.
PUBLIC_FIELDS = ("reg_id", "installation_id", "name", "kind", "state", "push_enabled", "last_result",
                 "last_ok_at", "last_seen_at", "registration_updated_at")


class PushRegistry:
    def __init__(self, hass: HomeAssistant, *, new_row_enabled: Callable[[], bool] | None = None) -> None:
        """`new_row_enabled` says whether a FIRST registration starts switched
        on (the push entry's "New phones get the push ring notification", read at each new
        row). Without it a new row starts OFF, as P1 did."""
        self._hass = hass
        self._new_row_enabled = new_row_enabled or (lambda: const.LEGACY_NEW_PHONES_ENABLED)
        self._store: Store[dict] = Store(hass, 1, const.REGISTRY_STORE_KEY, private=True, atomic_writes=True)
        self._rows: dict[str, dict[str, Any]] = {}
        self._listeners: list = []

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._rows = {row["reg_id"]: row for row in data.get("registrations", []) if row.get("reg_id")}

    def add_listener(self, listener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _changed(self) -> None:
        for listener in list(self._listeners):
            listener()

    def _data(self) -> dict:
        return {"registrations": list(self._rows.values())}

    async def _async_save(self) -> None:
        await self._store.async_save(self._data())
        self._changed()

    def _delay_save(self) -> None:
        self._store.async_delay_save(self._data, _SAVE_DELAY)
        self._changed()

    # ---- reads -------------------------------------------------------------

    def rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows.values()]

    def get(self, reg_id: str) -> dict[str, Any] | None:
        row = self._rows.get(reg_id)
        return dict(row) if row is not None else None

    def rows_for_user(self, ha_user_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows.values() if r["ha_user_id"] == ha_user_id]

    def targets(self) -> list[dict[str, Any]]:
        """Rows a ring may go to: switched on, with a live registration, not
        known dead or in the wrong project."""
        return [
            dict(r) for r in self._rows.values()
            if r.get("push_enabled") and r.get("registration")
            and r.get("state") not in (STATE_UNREGISTERED, STATE_PROJECT_MISMATCH)
        ]

    @staticmethod
    def is_stale(row: dict[str, Any], now: datetime | None = None) -> bool:
        seen = dt_util.parse_datetime(row.get("last_seen_at") or "")
        if seen is None:
            return False
        return (now or dt_util.utcnow()) - seen > STALE_AFTER

    @staticmethod
    def public(row: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        out = {k: row.get(k) for k in PUBLIC_FIELDS}
        out["stale"] = PushRegistry.is_stale(row, now)
        return out

    # ---- writes ------------------------------------------------------------

    async def async_register(
        self, *, ha_user_id: str, installation_id: str, kind: str, registration: str, package: str,
        fcm_project_id: str, app_version: str, name: str, enroll_id: str | None = None,
    ) -> dict[str, Any]:
        now = dt_util.utcnow().isoformat()
        row = next(
            (r for r in self._rows.values()
             if r["ha_user_id"] == ha_user_id and r["installation_id"] == installation_id),
            None,
        )
        if row is None:
            self._evict_for_new_row(ha_user_id)
            row = {
                "reg_id": uuid.uuid4().hex,
                "ha_user_id": ha_user_id,
                "installation_id": installation_id,
                "created_at": now,
                # Only a new row takes the default; an existing one keeps its switch.
                "push_enabled": bool(self._new_row_enabled()),
                "registration": None,
                "kind": None,
                "last_result": None,
                "last_ok_at": None,
            }
            self._rows[row["reg_id"]] = row
        if (row.get("kind"), row.get("registration")) != (kind, registration):
            row["registration_updated_at"] = now
            row["state"] = STATE_NEVER_SENT
            row["last_result"] = None
        row.update(kind=kind, registration=registration, package=package, fcm_project_id=fcm_project_id,
                   app_version=app_version, name=name, last_seen_at=now)
        if enroll_id:
            # PUSH-04: the phone entry this row belongs to. Not a public field (PUBLIC_FIELDS), never on the wire.
            row["enroll_id"] = enroll_id
        await self._async_save()
        return dict(row)

    def _evict_for_new_row(self, ha_user_id: str) -> None:
        """HA-L8: make room for a NEW installation of this user: drop the least recently seen rows until
        MAX_ROWS_PER_USER - 1 remain. The register reply has no field for it, so it is a WARNING in the log
        (the row's name only, never its registration). Never called for an existing installation."""
        mine = sorted((r for r in self._rows.values() if r["ha_user_id"] == ha_user_id),
                      key=lambda r: r.get("last_seen_at") or "")
        for old in mine[:max(0, len(mine) - (MAX_ROWS_PER_USER - 1))]:
            del self._rows[old["reg_id"]]
            _LOGGER.warning(
                "Push registry: user %s reached %d phones; evicted the least recently seen one (%r, last seen %s)",
                ha_user_id, MAX_ROWS_PER_USER, old.get("name"), old.get("last_seen_at"))

    async def async_unregister(self, ha_user_id: str, installation_id: str) -> bool:
        for reg_id, row in list(self._rows.items()):
            if row["ha_user_id"] == ha_user_id and row["installation_id"] == installation_id:
                del self._rows[reg_id]
                await self._async_save()
                return True
        return False

    async def async_remove(self, reg_id: str) -> bool:
        if self._rows.pop(reg_id, None) is None:
            return False
        await self._async_save()
        return True

    async def async_remove_user(self, ha_user_id: str) -> int:
        doomed = [k for k, r in self._rows.items() if r["ha_user_id"] == ha_user_id]
        for reg_id in doomed:
            del self._rows[reg_id]
        if doomed:
            await self._async_save()
        return len(doomed)

    async def async_remove_phone(self, enroll_id: str | None, ha_user_id: str) -> int:
        """PUSH-04: drop the rows of a removed phone entry: those stamped with its `enroll_id`, and, for rows
        registered before the stamp existed, those of its HA user. Returns how many."""
        doomed = [k for k, r in self._rows.items()
                  if (enroll_id and r.get("enroll_id") == enroll_id)
                  or (not r.get("enroll_id") and r.get("ha_user_id") == ha_user_id)]
        for reg_id in doomed:
            del self._rows[reg_id]
        if doomed:
            await self._async_save()
        return len(doomed)

    def rows_for_phone(self, enroll_id: str | None, ha_user_id: str) -> list[dict[str, Any]]:
        """The rows of one phone: stamped with its enroll id, or an unstamped row of its HA user."""
        return [dict(r) for r in self._rows.values()
                if (enroll_id and r.get("enroll_id") == enroll_id)
                or (not r.get("enroll_id") and r.get("ha_user_id") == ha_user_id)]

    async def async_set_enabled(self, reg_id: str, enabled: bool) -> bool:
        row = self._rows.get(reg_id)
        if row is None:
            return False
        row["push_enabled"] = bool(enabled)
        await self._async_save()
        return True

    async def async_age(self, now: datetime | None = None) -> int:
        """Delete rows not seen for 270 days; returns how many."""
        now = now or dt_util.utcnow()
        doomed = []
        for reg_id, row in self._rows.items():
            seen = dt_util.parse_datetime(row.get("last_seen_at") or "")
            if seen is not None and now - seen > EXPIRE_AFTER:
                doomed.append(reg_id)
        for reg_id in doomed:
            del self._rows[reg_id]
        if doomed:
            await self._async_save()
        return len(doomed)

    def record_result(self, reg_id: str, result: str, *, ok: bool, state: str | None = None) -> None:
        row = self._rows.get(reg_id)
        if row is None:
            return
        row["last_result"] = result
        if ok:
            row["last_ok_at"] = dt_util.utcnow().isoformat()
            row["state"] = STATE_OK
        if state is not None:
            row["state"] = state
        self._delay_save()

    def mark_dead(self, reg_id: str, result: str) -> None:
        """§2.4: clear the dead registration, keep the row as `unregistered`
        for the UI until the app registers again (or the row ages out)."""
        row = self._rows.get(reg_id)
        if row is None:
            return
        row["registration"] = None
        row["state"] = STATE_UNREGISTERED
        row["last_result"] = result
        self._delay_save()
