"""Phone/app enrollment — config-entry options flow (§4.7, §4.8, §9.5).

This is the *second* flow §4.7 describes: distinct from PorticoConfigFlow
(config_flow.py, device setup), it needs a config ENTRY to already exist,
so it's a `config_entries.OptionsFlow` hung off that entry
(PorticoConfigFlow.async_get_options_flow), not part of the initial
"Add Integration" wizard. Reached via Settings > Devices & Services >
this entry > Configure.

(UI split P1: the logic below lives in `enrollment_core.create_enrollment`, which this flow's steps call and whose
typed result they map to forms; the flow keeps the dialog: the Tailscale question, its credentials and the approval.)

One flow produces, in a single pass:

  1. A dedicated, non-admin, non-system-generated HA user for the phone
     (`hass.auth`, homeassistant/auth/__init__.py) that holds NO token yet,
     and a ONE-TIME CODE for it (DEV-544, enroll_redeem.py): the QR carries
     the code, not a token. The phone redeems the code over HTTP for a
     refresh token (24 h access tokens); the code is good for ten minutes
     and one redemption. The 10-year long-lived token this flow used to put
     in the QR is gone (cut over at once, the owner 2026-10-21).
  2. A Tailscale pre-authorized key, minted via Tailscale's own REST API
     (POST /api/v2/tailnet/{tailnet}/keys) using a credential the
     installer supplies once (see _step_tailscale_credentials below).
  3. Both packed into one QR code (a JSON payload, §4.7/§4.8 — "one flow,
     not pair with HA then separately set up Tailscale").

The flow hangs off the settings (push ring notification) entry's Configure ("Enroll a
phone"). Each enrolled phone becomes its OWN config entry (P-4, phone_entry.py):
the flow hands a `PhoneRecord` to the config flow's `phone` source
(`PorticoConfigFlow.async_step_phone`), which creates it. Only non-secret
identity goes in the entry's data: `ha_user_id`, `name`, `enrolled_at`,
`enroll_id` (a random id the QR also carries, so an offline
registration string can be matched to the phone; it is the only credential behind
offline registration, so diagnostics redact it), the enrollment `state`
(`pending` until the code is redeemed) and the token facts
(`token_kind`, `token_expires_at`, `refresh_state`). §9.5's `portico/devices`
keys its per-caller secrets scoping on `ActiveConnection.user`, which is
exactly this HA user id; the code and Tailscale key are shown once, in
the QR, and never re-displayed or persisted in HA (the code only as a keyed
digest, in memory).

Enrolling a phone whose name matches a phone that is still `pending` (or
`compromised`) is "Show a new QR" for it: same user, same `enroll_id`, the
earlier code dead at once. A name that matches a phone enrolled before
DEV-544 (it holds a ten-year token) is that phone being enrolled again: same
user and `enroll_id`, and its old token keeps working until the new QR is
redeemed, then is revoked (enroll_redeem._revoke_legacy_tokens). Deleting the phone's entry
revokes it (phone_entry.async_revoke_phone). The install-wide parts of the
answer (the Tailscale mode and its credentials, `ha_dial`) stay in the
settings entry's data, written directly via async_update_entry mid-flow (an
options flow's final async_create_entry replaces entry.options wholesale, so
the final step hands the entry's options back unchanged).

Tailscale credential storage (open question in §4.8 — resolved here):
an integration-level pair of fields (`tailscale_api_key`,
`tailscale_tailnet`, `tailscale_tag`) stored in the config entry's data,
collected once via `_step_tailscale_credentials` (skipped on every later
enrollment once present). Plaintext in entry data is exactly the same
storage tier this repo's own `unifi_protect` driver already uses for
`nvr_api_key` (drivers/unifi_protect.py) — not a new exposure, just the
established precedent for "a credential this integration needs to make
its own outbound API calls." A scoped Tailscale OAuth client (not a
full-account API key) is the safer real-world choice and is documented
in strings.json as the recommended credential type. The two are used
differently (`_async_mint_tailscale_authkey`): an OAuth client secret
(`tskey-client-...`) is exchanged for a one-hour access token first, an API
access token (`tskey-api-...`) is used directly.

Tailscale enrollment is now OPTIONAL, not mandatory (the owner, 2026-09-28:
"make this mechanism optional, if the user wants to create their own
tailscale enrollment in the app"). `async_step_tailscale_mode`, asked
once before anything else, offers `auto` (the flow above, unchanged) or
`manual` — which skips `async_step_tailscale_credentials` entirely, so
no Tailscale API credential is ever collected for that installer, and
the enrolment core (`enrollment_core`) mints no key and omits `tailscale_authkey`
from the QR payload. portico-app has no field to type a Tailscale key in
(a key reaches it only through the QR), so a `manual` phone does not join
the tailnet through Portico at all: it reaches HA another way (the home
network, or the official Tailscale app on the phone). The away path through
Portico's own tailnet client needs `approve` or `auto`. An entry created before this option existed
(has `tailscale_api_key` but no `tailscale_mode`) is backfilled to `auto`
silently — it already answered the question the only way that used to
exist.

`tailscale_mode` is written to entry data only once its answer is complete
(the owner, 2026-10-07): `manual` at once; `auto` only in the same
`async_update_entry` call as the credentials, when the credentials step is
submitted. An entry holding `auto` without a non-blank API key and tailnet
(left by an older version that saved the mode first) is treated as
unanswered and the question is asked again. The enrolment core mints
the Tailscale key BEFORE creating the HA user, and removes the user again if
anything after its creation fails, so a failed enrollment leaves no orphan
user.

`approve` (DEV-556, the owner 2026-10-07; the default for a new entry, listed
first): the HA admin approves each phone on Tailscale's own consent screen
(an OAuth app with the `auth_keys:create:once` scope, registered once in
`async_step_tailscale_oauth_app`), and the single-use key Tailscale returns
goes into the QR. Family phones need no Tailscale account. The mode is saved
only together with the app's client id and redirect URI in entry data and its
client secret in a 0600 private Store (tailscale_approve.OAuthAppStore); an
`approve` entry whose stored secret is missing is asked again. Per phone, the
approval (an external step) and the code exchange both happen BEFORE the HA
user is created. The callback view and its rules: tailscale_approve.py.
`auto` and `manual` are unchanged fallbacks.

Changing the mode (the owner, 2026-10-08): the settings entry's Configure menu
offers "Change the Tailscale enrollment mode" (`tailscale_change_mode`, the
same question with the current mode preselected) beside "Enroll a device". `approve` reuses what is
stored: its secret in the Store, its client id, and a stored redirect URI
equal to the one the CURRENT https URL gives are saved at once; otherwise
the same steps as the first time follow (a stale redirect URI through its own menu step,
`tailscale_oauth_app_choice_redirect_changed`, which says why). `auto`
always shows its credentials step (2026-10-08: a wrong tag stored with a
valid secret could not be corrected), the stored tailnet and tag pre-filled
and the secret optional while one is stored (blank keeps it). The mode is
written only together with what it needs, the dialog then ends with a
confirmation (`tailscale_mode_saved_<mode>`) and does not go on to a phone.
Switching away deletes nothing: the API credentials stay in entry data and
the OAuth app's secret in its Store (removing the entry still deletes it).

`ha_dial` (DEV-557): where the phone's tailnet path dials, an IP literal and
a port (ha_dial.py says why a name will not do). The phone form's optional
"Address phones use over Tailscale" field, validated with portico-app's own
rule (`parse_ha_dial`); blank means automatic: HA's own private IPv4 on its
default network adapter (`_auto_ha_dial`) with `ha_url`'s port. When neither
gives a valid value the key is left out and the app dials `ha_url`'s host.
A typed value is not secret and is kept in entry data as the next
enrollment's suggestion.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import network
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from . import enroll_redeem, enrollment_core, tailscale_approve
from .ha_dial import parse_ha_dial
from .phone_entry import PhoneRecord
from .push import const as push_const
from .push import get_manager
from .push.credentials import app_firebase_values

_LOGGER = logging.getLogger(__name__)

TAILSCALE_API_BASE = "https://api.tailscale.com/api/v2"

# Tailscale pre-auth key lifetime (`auto` mode; `approve` mode's one hour is
# fixed by Tailscale). It matches the life of the one-time code in the same QR
# (enroll_redeem.CODE_LIFETIME, 10 minutes, DEV-544 design section 2): a QR
# that is photographed and used later holds neither a code nor a key that
# still works. Ten minutes is ample for "scan the QR right after generating it".
_TAILSCALE_KEY_EXPIRY_SECONDS = int(enroll_redeem.CODE_LIFETIME.total_seconds())

# How long the key mint may take before the enrollment form shows
# `tailscale_mint_failed` instead of hanging.
_TAILSCALE_MINT_TIMEOUT_SECONDS = 30

# The enrollment payload's format marker. Format 2 (DEV-544) carries `code`, a
# one-time enrollment code, and NO `ha_token`; format 1 carried a ten-year
# token and is no longer produced. An app that does not know a format number
# refuses the QR with "update the app" (`EnrollmentPayload`, UNSUPPORTED_FORMAT)
# and saves nothing. Bump it only together with the app.
PAYLOAD_FORMAT = 2


# The `fcm` block's own format marker. portico-app (`EnrollmentPayload.parseFcm`)
# refuses a block whose `v` is present and not the number 1.
FCM_BLOCK_FORMAT = 1

# QR step ids: the normal one, and the same QR with a warning that the
# push ring notification values were left out (their own step so the text is translatable).
STEP_SHOW_QR = "show_qr"
STEP_SHOW_QR_FCM_SKIPPED = "show_qr_fcm_skipped"


# What a failed key mint can raise: an HTTP error status or a connection
# failure (aiohttp.ClientError), a timeout, a body that is not JSON
# (ValueError), or JSON without a string `key` (KeyError, TypeError).
_MINT_ERRORS = (
    aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, tailscale_approve.TailscaleRejected,
)


# The device tag a NEW `auto` answer suggests (the owner, 2026-10-08). Never
# another app's tag: a tag carries that app's ACL rules, and the OAuth client
# may apply only the tags it was created with.
DEFAULT_TAILSCALE_TAG = "tag:portico-phone"

# Only for an `auto` entry that stores no tag at all, which no version of the
# credentials step ever wrote (the tag field always had a default); the tag
# such an entry has minted with until now. Not a suggestion: never shown.
_LEGACY_TAILSCALE_TAG = "tag:intercom-phone"

# A Tailscale tag: `tag:` then letters, digits and dashes (ASCII only).
_TAG_RE = re.compile(r"tag:[A-Za-z0-9-]+")

# What a secret field shows while a secret is stored (the owner, 2026-10-09: "a
# row of stars to indicate that it's already filled in"): a suggested value,
# twelve bullets whatever the stored secret's length. Submitted back
# unchanged (or blank) it means "keep the stored secret". It cannot be a real
# secret (every Tailscale credential starts `tskey-`), and a value that still
# holds a bullet (typed after the stars without clearing them) is refused, so
# it is never stored, sent to Tailscale or logged.
SECRET_MASK = "\u2022" * 12
_MASK_CHAR = "\u2022"
_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

# Tailscale's error `message` on a 4xx, as logged: at most this many
# characters, after these are replaced with `[redacted]`: any Tailscale key or
# token (`tskey-...`), an Authorization value, and any long opaque run that
# could be a token of another shape.
_REFUSAL_MESSAGE_LIMIT = 200
_REFUSAL_BODY_LIMIT = 65536
_REDACT_PATTERNS = (
    re.compile(r"tskey-[A-Za-z0-9_-]+"),
    re.compile(r"(?i)\b(?:bearer|basic)\s+[^\s,;\"']+"),
    re.compile(r"[A-Za-z0-9_\-.+/=~]{32,}"),
)
_REDACTED = "[redacted]"


# The Tailscale question's answers, in the order the form lists them; the
# first is the default for a new entry (DEV-556).
TAILSCALE_MODES = ["approve", "auto", "manual"]

# The entry-data fields each mode needs before its answer counts as given.
# `approve` also needs its client secret in the private Store, which only the
# async check (`_async_tailscale_answer_complete`) can see.
_MODE_FIELDS = {
    "manual": (),
    "auto": ("tailscale_api_key", "tailscale_tailnet"),
    "approve": ("tailscale_client_id", "tailscale_redirect_uri"),
}


def _tailscale_answer_complete(data) -> bool:
    """Whether the entry data holds a finished answer to the Tailscale
    question: `manual`, `auto` WITH a non-blank API key and tailnet, or
    `approve` WITH a client id and redirect URI."""
    fields = _MODE_FIELDS.get(data.get("tailscale_mode"))
    if fields is None:
        return False
    return all(isinstance(data.get(f), str) and data[f].strip() for f in fields)


async def _async_tailscale_answer_complete(hass, entry: ConfigEntry, mode: str | None = None) -> bool:
    """`_tailscale_answer_complete`, and for `approve` its stored secret too.
    With `mode`: whether the entry holds everything THAT mode needs, whatever
    its current mode is (a change of mode reuses it)."""
    data = entry.data if mode is None else {**entry.data, "tailscale_mode": mode}
    if not _tailscale_answer_complete(data):
        return False
    if data["tailscale_mode"] != "approve":
        return True
    return await tailscale_approve.OAuthAppStore(hass, entry.entry_id).async_load_secret() is not None


def _scheme(url: str) -> str:
    try:
        return urlsplit(url).scheme.lower()
    except ValueError:
        return ""


def _select_ha_url(hass, *, allow_http: bool) -> tuple[str | None, str | None]:
    """(url, error key) for the QR's `ha_url` (the owner, S8): the external URL
    if it is https, else the internal URL if it is https; plain http (the
    external one first) only when the installer ticked "Allow plain http on
    my own network". `no_ha_url` when neither URL is set, `no_https_url` when
    only http is set and the box is off. Never an http URL by default.

    Reads `hass.config.external_url` / `internal_url` (Settings > System >
    Network) directly rather than `helpers.network.get_url`: get_url also
    falls back to the Nabu Casa cloud URL and to `http://<LAN IP>:<port>`
    built from the HTTP integration, and its preference between internal and
    external depends on whether HA itself serves TLS. The QR must carry a URL
    the installer configured, picked by one rule that a test can pin."""
    candidates = [
        url.strip()
        for url in (hass.config.external_url, hass.config.internal_url)
        if isinstance(url, str) and url.strip()
    ]
    if not candidates:
        return None, "no_ha_url"
    for url in candidates:
        if _scheme(url) == "https":
            return url, None
    if allow_http:
        for url in candidates:
            if _scheme(url) == "http":
                return url, None
    return None, "no_https_url"


def _select_redirect_uri(hass) -> tuple[str | None, str | None]:
    """(the OAuth redirect URI, error key) for `approve`: Portico's callback
    on the same https URL the QR would use, never plain http (the redirect
    carries the authorization code; the doc's examples are https only).
    `no_ha_url` / `no_https_url` as for the QR."""
    url, error = _select_ha_url(hass, allow_http=False)
    if error is not None:
        return None, error
    return url.rstrip("/") + tailscale_approve.CALLBACK_PATH, None


def _url_port(url: str) -> int | None:
    """`url`'s explicit port, else 443 for https and 80 for http; None for
    anything unreadable."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if port is not None:
        return port if 1 <= port <= 65535 else None
    return {"https": 443, "http": 80}.get(parts.scheme.lower())


def _url_target(url: str) -> str | None:
    """What portico-app dials without `ha_dial` (TailnetTarget.resolve's
    fallback): `ha_url`'s host and port, IPv6 bracketed. For the QR page."""
    port = _url_port(url)
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host or port is None:
        return None
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


async def _auto_ha_dial(hass, ha_url: str) -> str | None:
    """HA's own LAN address for `ha_dial`, or None.

    The rule: among HA's network adapters (Settings > System > Network,
    `homeassistant.components.network`), exactly one must be both the
    default-route adapter and enabled; on it, exactly one IPv4 address must
    pass `parse_ha_dial` (private, not loopback or link-local) when joined
    with `ha_url`'s port. Anything else (no such adapter, IPv6 only, a public
    address, two candidates) is None: a guess the phone would act on is worse
    than no value, and the installer can type one. Best effort: the helper
    failing is None too."""
    port = _url_port(ha_url)
    if port is None:
        return None
    try:
        adapters = await network.async_get_adapters(hass)
    except Exception as err:  # noqa: BLE001 - auto-detection must never block an enrollment
        _LOGGER.debug("Portico: no network adapters for ha_dial (%s)", type(err).__name__)
        return None
    defaults = [a for a in adapters if a.get("default") and a.get("enabled")]
    if len(defaults) != 1:
        return None
    found = {
        dial
        for address in defaults[0].get("ipv4") or []
        if isinstance(address, dict)
        and (dial := parse_ha_dial(f"{address.get('address')}:{port}")) is not None
    }
    return found.pop() if len(found) == 1 else None


def _fcm_block(hass, domain: str) -> tuple[dict[str, Any] | None, bool]:
    """(the `fcm` block or None, whether values were left out as unusable).

    The block (sender design §5.1, server P3) carries only the four Firebase
    client values, which are not secret; never anything of the service
    account. No push entry, or one without Firebase values: no block, so the
    key is absent and the app keeps what it has. Values portico-app's
    `FirebaseConfig.create` would refuse: no block either (a refused block
    refuses the whole enrollment), and the flow warns."""
    for entry in hass.config_entries.async_entries(domain):
        if entry.data.get(push_const.ENTRY_TYPE) != push_const.ENTRY_TYPE_PUSH:
            continue
        stored = entry.data.get("firebase")
        if stored is None:
            return None, False
        values = app_firebase_values(stored)
        if values is None:
            _LOGGER.warning(
                "Portico: the push ring notification entry's Firebase values are not ones the app accepts; "
                "they were left out of the enrollment QR. Re-add google-services.json to fix it"
            )
            return None, True
        return {"v": FCM_BLOCK_FORMAT, **values}, False
    return None, False


class PorticoOptionsFlow(config_entries.OptionsFlow):
    """Phone/app enrollment for an already-configured Portico device."""

    def __init__(self) -> None:
        self._phone_name: str | None = None
        # Populated once creation succeeds; consumed by the final
        # "show_qr" step then discarded — never written to entry data.
        self._qr_data_uri: str | None = None
        self._ha_user_id: str | None = None
        # When the code in the QR stops working (UTC), and the QR page's note about an earlier phone it replaces.
        self._code_expires: datetime | None = None
        # The enrolled phone a new QR would replace: being asked about, and the one the admin confirmed.
        self._confirming = None
        self._reuse_confirmed: str | None = None
        self._reenroll_note = ""
        self._allow_http = False
        self._fcm_skipped = False
        # The phone form's "Address phones use over Tailscale", canonical;
        # None means automatic. Not secret.
        self._ha_dial_typed: str | None = None
        # The same as typed, trimmed, for the core to parse (and for the form to show again when refused).
        self._ha_dial_raw: str = ""
        # What the QR page names as the tailnet target.
        self._dial_target: str | None = None
        # `auto` chosen on the mode step, held until the credentials step
        # writes it to entry data together with the credentials.
        self._pending_tailscale_mode: str | None = None
        # `approve`: the redirect URI shown for registration on the OAuth-app
        # steps; per enrollment, the issued approval state's record and its
        # authorization URL, and the key the exchange returned (held only
        # until the user is created).
        self._redirect_uri: str | None = None
        self._approval: tailscale_approve.ApprovalRecord | None = None
        self._approval_url: str | None = None
        self._approved_authkey: str | None = None
        # Set when the installer picked "Change the Tailscale enrollment
        # mode": a saved answer then ends the dialog instead of going on to
        # the phone form.
        self._changing_mode = False

    async def _async_enrollment_gate(self):
        """The Tailscale question when this entry has no complete answer yet
        (that step's result), else None: enrollment may go on. Used by the
        settings entry's "Enroll a device" (push_flow.PushOptionsFlow)."""
        entry = self.config_entry
        if "tailscale_mode" not in entry.data:
            # An entry from before this option existed already has
            # `tailscale_api_key` (the only mode there used to be) —
            # backfill `tailscale_mode` rather than re-asking a question
            # that already has an answer on disk.
            if "tailscale_api_key" not in entry.data:
                return await self.async_step_tailscale_mode()
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, "tailscale_mode": "auto"}
            )
        if not await _async_tailscale_answer_complete(self.hass, entry):
            # `auto` without its credentials, or `approve` without its OAuth
            # app (its secret missing from the Store): an unfinished answer,
            # not an answer (an older version saved the mode before the
            # credentials step, the owner 2026-10-07). Ask again, the entry left
            # as it is.
            return await self.async_step_tailscale_mode()
        return None

    async def async_step_tailscale_change_mode(self, user_input: dict[str, Any] | None = None):
        """The mode question again, the current mode preselected. Nothing is
        written until the chosen mode has what it needs."""
        self._changing_mode = True
        if user_input is not None:
            return await self._async_choose_mode(user_input["tailscale_mode"], "tailscale_change_mode")
        return self._show_tailscale_mode(
            step_id="tailscale_change_mode", default=self.config_entry.data.get("tailscale_mode")
        )

    async def _async_mode_saved(self, mode: str):
        """The answer is on disk: the first time, go on to the phone; after a
        change, end the dialog with a confirmation."""
        if self._changing_mode:
            return self.async_abort(reason=f"tailscale_mode_saved_{mode}")
        return await self.async_step_phone_details()

    def _write_mode(self, mode: str, **extra: Any) -> None:
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self.config_entry.data, **extra, "tailscale_mode": mode}
        )

    async def _async_choose_mode(self, mode: str, step_id: str):
        """A submitted answer to the mode question (`step_id` is the form it
        came from, for its errors)."""
        data = self.config_entry.data
        if mode == "approve":
            # Persisted only once the OAuth app is stored (both or neither).
            redirect_uri, error = _select_redirect_uri(self.hass)
            if error is not None:
                return self._show_tailscale_mode(
                    step_id=step_id,
                    default=data.get("tailscale_mode") if self._changing_mode else None,
                    errors={"base": error},
                )
            self._redirect_uri = redirect_uri
            if self._changing_mode:
                stored_uri = data.get("tailscale_redirect_uri")
                app_complete = await _async_tailscale_answer_complete(
                    self.hass, self.config_entry, mode="approve"
                )
                if app_complete and stored_uri == redirect_uri:
                    self._write_mode("approve")
                    return await self._async_mode_saved("approve")
                if app_complete:
                    # Registered on the app is the OLD URI: Tailscale would
                    # refuse the new one, so the app is set up again.
                    return await self.async_step_tailscale_oauth_app_choice_redirect_changed()
            return await self.async_step_tailscale_oauth_app_choice()
        if mode == "auto":
            # Persisted only together with the credentials (next step):
            # an `auto` saved alone, then abandoned, was the 2026-10-07
            # bug — the question never came back. On a change the step is
            # shown even with complete stored credentials, pre-filled, so a
            # wrong tag or tailnet can be corrected (2026-10-08).
            self._pending_tailscale_mode = mode
            return await self.async_step_tailscale_credentials()
        self._write_mode(mode)
        return await self._async_mode_saved(mode)

    @callback
    def async_remove(self) -> None:
        """The flow closed (finished or abandoned): its approval state, if
        any, stops being valid, and an unused key is dropped."""
        tailscale_approve.get_registry(self.hass).revoke_flow(self.flow_id)
        self._approval = None
        self._approved_authkey = None

    async def async_step_tailscale_mode(self, user_input: dict[str, Any] | None = None):
        """One-time, asked before anything else: does THIS installer want
        Portico to mint a Tailscale key per phone (the original design,
        §4.8), or would they rather each phone join the tailnet on its
        own — the owner, 2026-09-28: "make this mechanism optional, if the
        user wants to create their own tailscale enrollment in the app."

        `manual` skips `async_step_tailscale_credentials` entirely — no
        Tailscale API credential is ever collected or stored for that
        installer, and the QR carries no Tailscale key: the phone does not
        join the tailnet through Portico (portico-app has no field to type
        a key in) and reaches HA another way.

        `approve` (DEV-556), listed first and the default: the HA admin
        approves each phone on Tailscale's consent screen; set up once in
        the OAuth-app steps below, persisted only when they finish."""
        if user_input is not None:
            return await self._async_choose_mode(user_input["tailscale_mode"], "tailscale_mode")
        return self._show_tailscale_mode()

    def _show_tailscale_mode(
        self, *, step_id: str = "tailscale_mode", default: str | None = None, errors: dict[str, str] | None = None
    ):
        if default not in TAILSCALE_MODES:
            default = TAILSCALE_MODES[0]
        schema = vol.Schema(
            {
                vol.Required("tailscale_mode", default=default): SelectSelector(
                    SelectSelectorConfig(
                        options=list(TAILSCALE_MODES),
                        translation_key="tailscale_mode",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)

    async def async_step_tailscale_oauth_app_choice(self, user_input: dict[str, Any] | None = None):
        """`approve`, one-time: paste the OAuth app's secret, or let Portico
        create the app with a one-time admin API access token."""
        return self.async_show_menu(
            step_id="tailscale_oauth_app_choice",
            menu_options=["tailscale_oauth_app", "tailscale_oauth_app_create"],
            description_placeholders=self._oauth_app_placeholders(),
        )

    async def async_step_tailscale_oauth_app_choice_redirect_changed(
        self, user_input: dict[str, Any] | None = None
    ):
        """A change to `approve` whose stored app was registered with a
        redirect URI that is no longer the current one (the https URL
        changed): the same choice, saying why it is asked again."""
        return self.async_show_menu(
            step_id="tailscale_oauth_app_choice_redirect_changed",
            menu_options=["tailscale_oauth_app", "tailscale_oauth_app_create"],
            description_placeholders={
                **self._oauth_app_placeholders(),
                "old_redirect_uri": str(self.config_entry.data.get("tailscale_redirect_uri") or ""),
            },
        )

    def _oauth_app_placeholders(self) -> dict[str, str]:
        return {"redirect_uri": self._redirect_uri or ""}

    async def async_step_tailscale_oauth_app(self, user_input: dict[str, Any] | None = None):
        """`approve`, one-time: the OAuth app's client secret (and, only if
        the secret does not show it, its client id).

        While a secret is in the Store (the app set up again because the
        redirect URI changed) the field shows SECRET_MASK and is optional:
        the stars or a blank keep the stored secret, with the typed client id
        or else the stored one; a typed secret replaces it."""
        errors: dict[str, str] = {}
        stored = await tailscale_approve.OAuthAppStore(self.hass, self.config_entry.entry_id).async_load_secret()
        if user_input is not None:
            typed, mask_error = _typed_secret(user_input.get("client_secret"))
            client_id_typed = _stripped(user_input.get("client_id"))
            if mask_error:
                errors["client_secret"] = mask_error
            else:
                if not typed and stored is not None:
                    candidate = stored
                    client_id_typed = client_id_typed or _stripped(self.config_entry.data.get("tailscale_client_id"))
                else:
                    candidate = typed
                try:
                    client_id, secret = tailscale_approve.parse_client_secret(candidate, client_id_typed or None)
                except ValueError as err:
                    field = str(err) if str(err) in ("client_secret", "client_id") else "client_secret"
                    errors[field] = f"tailscale_{field}_invalid"
                else:
                    return await self._async_save_oauth_app(client_id, secret)
        # No defaults: a refused secret is never offered back in the form.
        schema = vol.Schema(
            {
                _secret_key("client_secret", stored is not None): _PASSWORD,
                vol.Optional("client_id"): str,
            }
        )
        return self.async_show_form(
            step_id="tailscale_oauth_app",
            data_schema=schema,
            errors=errors,
            description_placeholders=self._oauth_app_placeholders(),
        )

    async def async_step_tailscale_oauth_app_create(self, user_input: dict[str, Any] | None = None):
        """`approve`, one-time, optional: Portico creates the OAuth app with
        an admin API access token that is used for this one request and
        stored nowhere (Tailscale doc, step 1: `curl -u "<api-access-token>:"`)."""
        errors: dict[str, str] = {}
        if user_input is not None and self._redirect_uri:
            try:
                secret, app_id = await tailscale_approve.async_create_oauth_app(
                    self.hass,
                    api_access_token=user_input["api_access_token"].strip(),
                    redirect_uri=self._redirect_uri,
                )
                client_id, secret = tailscale_approve.parse_client_secret(secret, app_id)
            except tailscale_approve.TailscaleRejected as err:
                _LOGGER.warning(
                    "Portico: Tailscale refused to create the OAuth app (HTTP status %s)", err.status
                )
                errors["base"] = "tailscale_api_token_rejected"
            except tailscale_approve.HTTP_ERRORS as err:
                _LOGGER.warning(
                    "Portico: could not create the Tailscale OAuth app (%s, HTTP status %s)",
                    type(err).__name__,
                    getattr(err, "status", None),
                )
                errors["base"] = "tailscale_create_app_failed"
            else:
                return await self._async_save_oauth_app(client_id, secret)
        schema = vol.Schema(
            {
                # Stored nowhere, so never the stars.
                vol.Required("api_access_token"): _PASSWORD,
            }
        )
        return self.async_show_form(
            step_id="tailscale_oauth_app_create",
            data_schema=schema,
            errors=errors,
            description_placeholders=self._oauth_app_placeholders(),
        )

    async def _async_save_oauth_app(self, client_id: str, secret: str):
        """The secret to the private Store first, then the mode, client id and
        redirect URI to entry data in one write: if the Store write fails the
        entry is untouched, so `approve` is never on disk without its app."""
        await tailscale_approve.OAuthAppStore(self.hass, self.config_entry.entry_id).async_save_secret(secret)
        self._write_mode("approve", tailscale_client_id=client_id, tailscale_redirect_uri=self._redirect_uri)
        return await self._async_mode_saved("approve")

    async def async_step_tailscale_approve(self, user_input: dict[str, Any] | None = None):
        """`approve`, per phone: the admin approves on Tailscale's consent
        screen. user_input is IGNORED: only the callback view, holding the
        state this flow issued, can complete this step (it sets the record's
        result, then resumes the flow)."""
        record = self._approval
        if record is not None and record.result is not None:
            return self.async_external_step_done(next_step_id="tailscale_approved")
        if record is None:
            tailscale_approve.async_register_view(self.hass)
            state, self._approval = tailscale_approve.get_registry(self.hass).issue(self.flow_id)
            self._approval_url = tailscale_approve.authorize_url(
                self.config_entry.data["tailscale_client_id"],
                self.config_entry.data["tailscale_redirect_uri"],
                state,
            )
        assert self._approval_url is not None
        return self.async_external_step(step_id="tailscale_approve", url=self._approval_url)

    async def async_step_tailscale_approved(self, user_input: dict[str, Any] | None = None):
        """After the callback: the code for a single-use key, then on to the
        HA user. Every failure lands on the phone form, nothing created."""
        record, self._approval, self._approval_url = self._approval, None, None
        tailscale_approve.get_registry(self.hass).revoke_flow(self.flow_id)
        if record is None or record.result is None or record.result[0] != "code":
            return self._show_phone_details(errors={"base": "tailscale_approval_denied"})
        entry = self.config_entry
        secret = await tailscale_approve.OAuthAppStore(self.hass, entry.entry_id).async_load_secret()
        if secret is None or not _tailscale_answer_complete(entry.data):
            # The OAuth app went missing under us: the answer is unfinished.
            return await self.async_step_tailscale_mode()
        try:
            self._approved_authkey = await tailscale_approve.async_exchange_code(
                self.hass,
                client_id=entry.data["tailscale_client_id"],
                client_secret=secret,
                code=record.result[1],
                redirect_uri=entry.data["tailscale_redirect_uri"],
            )
        except tailscale_approve.TailscaleRejected as err:
            _LOGGER.warning(
                "Portico: Tailscale refused the approval code for entry %s (HTTP status %s)",
                entry.entry_id,
                err.status,
            )
            return self._show_phone_details(errors={"base": "tailscale_exchange_rejected"})
        except tailscale_approve.HTTP_ERRORS as err:
            _LOGGER.warning(
                "Portico: could not exchange the Tailscale approval code for entry %s (%s, HTTP status %s)",
                entry.entry_id,
                type(err).__name__,
                getattr(err, "status", None),
            )
            return self._show_phone_details(errors={"base": "tailscale_exchange_failed"})
        return await self._async_enroll()

    async def async_step_tailscale_credentials(self, user_input: dict[str, Any] | None = None):
        """The Tailscale credential this integration mints keys with (§4.8's
        open question — see this module's docstring for the resolution).
        Only reached when `auto` is chosen; skipped on every enrollment after
        the first.

        The first time: an empty form, the tag defaulting to
        DEFAULT_TAILSCALE_TAG. On "Change the Tailscale enrollment mode"
        (2026-10-08): always shown, the stored tailnet and tag pre-filled.
        While a secret is stored the secret field shows SECRET_MASK (stars,
        2026-10-09) and is optional: the stars or a blank keep the stored
        secret, a typed one replaces it. Every field is stripped; a blank
        tailnet or tag, or a tag not shaped `tag:<letters, digits, dashes>`,
        is refused."""
        stored_secret = self._stored_tailscale_secret()
        if user_input is not None:
            values, errors = _check_tailscale_credentials(user_input, stored_secret)
            if errors:
                return self._show_tailscale_credentials(
                    errors=errors,
                    tailnet=str(user_input.get("tailscale_tailnet") or ""),
                    tag=str(user_input.get("tailscale_tag") or ""),
                )
            mode = self._pending_tailscale_mode or "auto"
            # The mode and its credentials in one write: either all are on
            # disk or none is.
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, **values, "tailscale_mode": mode},
            )
            self._pending_tailscale_mode = None
            return await self._async_mode_saved(mode)
        if not self._changing_mode:
            return self._show_tailscale_credentials()
        data = self.config_entry.data
        return self._show_tailscale_credentials(
            tailnet=_stripped(data.get("tailscale_tailnet")) or None,
            tag=_stripped(data.get("tailscale_tag")) or DEFAULT_TAILSCALE_TAG,
        )

    def _stored_tailscale_secret(self) -> str | None:
        secret = _stripped(self.config_entry.data.get("tailscale_api_key"))
        return secret or None

    def _show_tailscale_credentials(
        self, *, errors: dict[str, str] | None = None, tailnet: str | None = None, tag: str | None = None
    ):
        """The form. `tailnet` / `tag` given: suggested (a change of mode, or
        what was just typed and refused), the tag then without a default so a
        cleared field is refused rather than silently replaced. The secret is
        never offered back: a stored one shows as the stars."""
        edit = self._changing_mode or errors is not None
        secret_key = _secret_key("tailscale_api_key", self._stored_tailscale_secret() is not None)
        if not edit:
            schema = vol.Schema(
                {
                    secret_key: _PASSWORD,
                    vol.Required("tailscale_tailnet"): str,
                    vol.Optional("tailscale_tag", default=DEFAULT_TAILSCALE_TAG): str,
                }
            )
        else:
            schema = vol.Schema(
                {
                    secret_key: _PASSWORD,
                    vol.Required("tailscale_tailnet", description=_suggest(tailnet)): str,
                    vol.Optional("tailscale_tag", description=_suggest(tag)): str,
                }
            )
        return self.async_show_form(step_id="tailscale_credentials", data_schema=schema, errors=errors)

    async def async_step_phone_details(self, user_input: dict[str, Any] | None = None):
        """Step: name this phone, then enroll it."""
        if user_input is not None:
            self._phone_name = user_input["phone_name"]
            self._allow_http = bool(user_input.get("allow_http", False))
            typed = (user_input.get("ha_dial") or "").strip()
            self._ha_dial_typed = parse_ha_dial(typed) if typed else None
            self._ha_dial_raw = typed
            return await self._async_enroll()
        return self._show_phone_details()

    async def _async_enroll(self):
        """Run the enrolment core (enrollment_core.create_enrollment: the whole of what enrolling does, in its order)
        and map its typed result to this dialog's steps. Called from the phone form, again once a replacement was
        confirmed, and again once the Tailscale approval produced a key."""
        authkey, self._approved_authkey = self._approved_authkey, None
        result = await enrollment_core.create_enrollment(
            self.hass,
            self.config_entry,
            name=self._phone_name or "Portico phone",
            allow_http=self._allow_http,
            ha_dial_raw=self._ha_dial_raw,
            confirmed_entry_id=self._reuse_confirmed,
            approved_authkey=authkey,
        )
        if isinstance(result, enrollment_core.Created):
            self._qr_data_uri = f"data:image/png;base64,{base64.b64encode(result.qr_png).decode('ascii')}"
            self._ha_user_id = result.ha_user_id
            self._code_expires = result.expires_at
            self._reenroll_note = self._note_for(result.notes)
            self._fcm_skipped = enrollment_core.NOTE_FCM_SKIPPED in result.notes
            self._dial_target = result.dial_target
            return await self.async_step_show_qr()
        if isinstance(result, enrollment_core.NeedsConfirmReenroll):
            self._confirming = self.hass.config_entries.async_get_entry(result.entry_id)
            return self._show_confirm_reenroll(self._confirming)
        if isinstance(result, enrollment_core.NeedsTailscaleSetup):
            return await self.async_step_tailscale_mode()
        if isinstance(result, enrollment_core.NeedsApproval):
            # Approval and exchange first; they come back here.
            return await self.async_step_tailscale_approve()
        # An Error. Whatever went wrong inside is the exception the dialog always raised.
        if result.exception is not None:
            raise result.exception
        if result.code == enrollment_core.ERR_INVALID_HA_DIAL:
            return self._show_phone_details(errors={"ha_dial": result.code}, ha_dial=self._ha_dial_raw)
        if result.code == enrollment_core.ERR_NO_PUSH_ENTRY:
            raise RuntimeError("the enrollment dialog has no push ring notification entry")
        return self._show_phone_details(errors={result.field or "base": result.code})

    def _show_confirm_reenroll(self, phone_entry):
        record = PhoneRecord.from_data(phone_entry.data)
        enrolled_on = dt_util.parse_datetime(record.enrolled_at)
        seen = ""
        manager = get_manager(self.hass)
        if manager is not None:
            rows = manager.registry.rows_for_phone(record.enroll_id, record.ha_user_id)
            stamps = sorted(r["last_seen_at"] for r in rows if r.get("last_seen_at"))
            seen = stamps[-1] if stamps else ""
        last = dt_util.parse_datetime(seen) if seen else None
        return self.async_show_form(
            step_id="confirm_reenroll",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            description_placeholders={
                "phone_name": record.name,
                "enrolled_on": dt_util.as_local(enrolled_on).strftime("%Y-%m-%d") if enrolled_on else "unknown",
                "last_seen": dt_util.as_local(last).strftime("%Y-%m-%d %H:%M") if last else "never or unknown",
            },
        )

    async def async_step_confirm_reenroll(self, user_input: dict[str, Any] | None = None):
        """The answer to the core's `NeedsConfirmReenroll`: ticking the box goes on, leaving it off goes back to the name."""
        if user_input is None or self._confirming is None:
            return self._show_phone_details()
        confirming, self._confirming = self._confirming, None
        if not user_input.get("confirm"):
            return self._show_phone_details()
        self._reuse_confirmed = confirming.entry_id
        return await self._async_enroll()

    def _show_phone_details(self, errors: dict[str, str] | None = None, ha_dial: str | None = None):
        # The suggestion: what was just typed (also when refused, to fix it),
        # else this flow's earlier answer, else the entry's remembered one.
        suggested = ha_dial or self._ha_dial_typed or parse_ha_dial(self.config_entry.data.get("ha_dial"))
        schema = vol.Schema(
            {
                vol.Required("phone_name", default=self._phone_name or ""): str,
                # Off unless the installer ticks it, every time (S8).
                vol.Required("allow_http", default=self._allow_http): bool,
                # Blank: automatic (DEV-557).
                vol.Optional("ha_dial", description={"suggested_value": suggested} if suggested else None): str,
            }
        )
        return self.async_show_form(step_id="phone_details", data_schema=schema, errors=errors)

    @staticmethod
    def _note_for(notes) -> str:
        """The QR page's line about an earlier phone this QR replaces; empty for a new phone."""
        if enrollment_core.NOTE_REENROLL_LONG_LIVED in notes:
            return (
                "\n\n**This phone is already enrolled with a long-lived token.** Scanning this QR enrolls it again: "
                "its old token keeps working until this QR is used, then stops."
            )
        if enrollment_core.NOTE_REPLACES_EARLIER in notes:
            return "\n\n**This replaces the earlier QR for this phone;** the earlier one no longer works."
        return ""

    async def async_step_show_qr_fcm_skipped(self, user_input: dict[str, Any] | None = None):
        """The QR step's submit when it was shown with the push ring notification
        warning (its own step id, so the warning text is translatable)."""
        return await self.async_step_show_qr(user_input)

    async def async_step_show_qr(self, user_input: dict[str, Any] | None = None):
        """Final step: display the QR once. Nothing sensitive is kept
        after this — closing/re-opening this flow can't re-show it."""
        if user_input is not None:
            # Options flows persist via async_create_entry's `data`
            # (OptionsFlowManager.async_finish_flow overwrites
            # entry.options with it wholesale), and this flow runs on the
            # push entry, whose options are the push ring notification's settings
            # (test phones, new-phones, ring window). The enrollment state
            # lives in entry.data (written above, directly), so hand the
            # options back unchanged.
            return self.async_create_entry(title="", data=dict(self.config_entry.options))

        assert self._qr_data_uri is not None  # only reachable via _async_enroll
        manual_note = (
            "\n\n⚠️ **This QR carries no Tailscale key.** This device's "
            "Tailscale enrollment is manual, so the phone does not join your "
            "tailnet through Portico. It reaches Home Assistant only another "
            "way: on the home network, or through the official Tailscale app "
            "on the phone. To reach Home Assistant away from home through "
            "Portico's own Tailscale, choose Approve in Tailscale or Mint keys "
            "under Configure > \"Change the Tailscale enrollment mode\", then "
            "enroll this phone again."
            if self.config_entry.data.get("tailscale_mode") == "manual"
            else ""
        )
        return self.async_show_form(
            step_id=STEP_SHOW_QR_FCM_SKIPPED if self._fcm_skipped else STEP_SHOW_QR,
            data_schema=vol.Schema({}),
            description_placeholders={
                "phone_name": self._phone_name or "",
                "qr_image": f"![Enrollment QR]({self._qr_data_uri})",
                "manual_note": manual_note,
                "expires_at": (
                    dt_util.as_local(self._code_expires).strftime("%H:%M") if self._code_expires is not None else ""
                ),
                "reenroll_note": self._reenroll_note,
                "ha_dial": f"`{self._dial_target}`" if self._dial_target else "—",
            },
        )


def _stripped(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _suggest(value: str | None) -> dict[str, str] | None:
    return {"suggested_value": value} if value else None


def _secret_key(name: str, stored: bool) -> vol.Marker:
    """A secret field: while a secret is stored, optional and showing the
    stars; otherwise empty and required. Never the secret itself."""
    if stored:
        return vol.Optional(name, description={"suggested_value": SECRET_MASK})
    return vol.Required(name)


def _typed_secret(value: Any) -> tuple[str, str | None]:
    """(the typed secret, error key) of a secret field: "" for blank or the
    untouched stars (keep the stored secret, if any); `secret_mask_edited`
    for a value that still holds a bullet. Stripped."""
    typed = _stripped(value)
    if typed == SECRET_MASK:
        return "", None
    if _MASK_CHAR in typed:
        return "", "secret_mask_edited"
    return typed, None


def _check_tailscale_credentials(
    user_input: dict[str, Any], stored_secret: str | None
) -> tuple[dict[str, str], dict[str, str]]:
    """(the three fields to store, errors) for a submitted credentials form.
    `stored_secret`: kept when the secret field is blank or the untouched
    stars (None: the secret is required, and the stars count as blank)."""
    errors: dict[str, str] = {}
    typed, mask_error = _typed_secret(user_input.get("tailscale_api_key"))
    secret = typed or stored_secret
    if mask_error:
        errors["tailscale_api_key"] = mask_error
    elif not secret:
        errors["tailscale_api_key"] = "tailscale_api_key_blank"
    tailnet = _stripped(user_input.get("tailscale_tailnet"))
    if not tailnet:
        errors["tailscale_tailnet"] = "tailscale_tailnet_blank"
    tag = _stripped(user_input.get("tailscale_tag"))
    if not tag:
        errors["tailscale_tag"] = "tailscale_tag_blank"
    elif not _TAG_RE.fullmatch(tag):
        errors["tailscale_tag"] = "tailscale_tag_invalid"
    if errors:
        return {}, errors
    return {"tailscale_api_key": secret, "tailscale_tailnet": tailnet, "tailscale_tag": tag}, {}


def _redacted_message(message: str, credentials: tuple[str, ...]) -> str:
    """Tailscale's error message, safe for the log: on one line, every
    credential and anything token-shaped replaced, then truncated."""
    text = " ".join("".join(c if c.isprintable() else " " for c in message).split())
    for credential in credentials:
        if credential:
            text = text.replace(credential, _REDACTED)
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text[:_REFUSAL_MESSAGE_LIMIT]


async def _async_log_refusal(resp, what: str, credentials: tuple[str, ...]) -> None:
    """A 4xx from Tailscale: ONE warning with the status and Tailscale's own
    error `message`, only when the body is a JSON object with a string
    `message` (redacted, truncated). Never the request, its headers, the
    credential or the rest of the body; any other body logs nothing here."""
    try:
        raw = await resp.read()
        if len(raw) > _REFUSAL_BODY_LIMIT:
            return
        body = json.loads(raw)
    except Exception:  # noqa: BLE001 - a diagnostic must never change the outcome
        return
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, str):
        return
    text = _redacted_message(message, credentials)
    if text:
        _LOGGER.warning("Portico: Tailscale's answer to the %s (HTTP status %s): %s", what, resp.status, text)


# An OAuth client secret (Tailscale docs, "Key prefixes": `tskey-client`
# is "a Tailscale OAuth client key"). It is not an API credential itself: it
# is exchanged for a one-hour access token first (docs/features/oauth-clients).
_OAUTH_CLIENT_SECRET_PREFIX = "tskey-client-"


def _oauth_client_id(secret: str) -> str:
    """The client id embedded in `tskey-client-<id>-<secret>`.

    Tailscale's documentation does not spell the secret's layout out; the
    `<id>-<secret>` shape is what Tailscale-issued client secrets look like
    (as with the `tskey-app-<id>-<secret>` OAuth app secrets, see
    tailscale_approve.parse_client_secret). Tailscale's own client
    (feature/oauthkey) sends a placeholder client id, marked "ignored", next
    to the secret, so the server identifies the client by the secret; the id
    sent here is best-effort. ValueError (no value in the message) when the
    secret does not have that shape."""
    found, dash, tail = secret[len(_OAUTH_CLIENT_SECRET_PREFIX):].partition("-")
    if not dash or not found or not tail:
        raise ValueError("malformed OAuth client secret")
    return found


async def _async_oauth_access_token(session, client_secret: str) -> str:
    """POST /api/v2/oauth/token, client credentials grant: the client secret
    for a short-lived (one hour) API access token. tailscale_approve's
    TailscaleRejected on a 4xx (a refused client, its message logged by
    _async_log_refusal), _MINT_ERRORS otherwise."""
    client_id = _oauth_client_id(client_secret)
    async with session.post(
        tailscale_approve.TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=aiohttp.ClientTimeout(total=_TAILSCALE_MINT_TIMEOUT_SECONDS),
    ) as resp:
        if 400 <= resp.status < 500:
            await _async_log_refusal(resp, "token exchange", (client_secret,))
            raise tailscale_approve.TailscaleRejected(resp.status)
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    token = data["access_token"]
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise ValueError("Tailscale returned no usable access token")
    return token


async def _async_mint_tailscale_authkey(hass, *, api_key: str, tailnet: str, tag: str) -> str:
    """POST /api/v2/tailnet/{tailnet}/keys — a real, single-use,
    pre-authorized, tagged key that expires in an hour (Tailscale's
    documented REST API, §4.8).

    `api_key` is either an OAuth client secret (`tskey-client-...`), which is
    first exchanged for an access token used as a bearer, or an API access
    token (`tskey-api-...`, or anything else), used directly with HTTP Basic
    auth as before. A 4xx logs Tailscale's own error message, redacted
    (_async_log_refusal); the caller logs the exception class and HTTP
    status."""
    session = aiohttp_client.async_get_clientsession(hass)
    token = ""
    if api_key.startswith(_OAUTH_CLIENT_SECRET_PREFIX):
        token = await _async_oauth_access_token(session, api_key)
        authorization = f"Bearer {token}"
    else:
        # HTTP Basic, the token as the user name, an empty password: the
        # same request aiohttp.BasicAuth(api_key, "") made.
        authorization = "Basic " + base64.b64encode(f"{api_key}:".encode()).decode("ascii")
    url = f"{TAILSCALE_API_BASE}/tailnet/{tailnet}/keys"
    body = {
        "capabilities": {
            "devices": {
                "create": {
                    "reusable": False,
                    # NOT ephemeral: Tailscale auto-removes an ephemeral node
                    # "anywhere normally from 30 to 60 minutes after the last
                    # activity" (docs/features/ephemeral-nodes), so a phone
                    # asleep for an hour would leave the tailnet, and its
                    # single-use key could not bring it back. The key is
                    # single-use and expires in an hour; the node persists
                    # until removed in the admin console (Machines).
                    "ephemeral": False,
                    "preauthorized": True,
                    # An OAuth client's keys must carry tags, named here too
                    # (docs/features/oauth-clients).
                    "tags": [tag],
                }
            }
        },
        "expirySeconds": _TAILSCALE_KEY_EXPIRY_SECONDS,
    }
    async with session.post(
        url,
        json=body,
        headers={"Authorization": authorization},
        timeout=aiohttp.ClientTimeout(total=_TAILSCALE_MINT_TIMEOUT_SECONDS),
    ) as resp:
        if 400 <= resp.status < 500:
            await _async_log_refusal(resp, "key request", (api_key, token))
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    key = data["key"]
    if not isinstance(key, str) or not key:
        raise ValueError("Tailscale returned no usable key")
    return key


def _qr_text(payload: dict[str, Any]) -> str:
    """The QR's text: compact JSON, non-ASCII kept as itself. `ensure_ascii`
    would write each Hebrew letter of a phone name as a 6-byte `\\u05xx`
    escape instead of 2 UTF-8 bytes, a larger, denser QR (sender design
    §5.4)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# Pixels per QR module in the PNG (DEV-673). HA's flow dialog shows a Markdown image at its natural size (capped to the
# dialog's width) and scrolls; the old 10 px made a ~930 px PNG (version 17, 85 modules + 4-module border) that
# HA scaled to ~500 px, its lower part below the fold, where a phone camera cannot scroll to it. 4 px gives 372 px for
# the realistic 85-module code, 4 css px per module.
QR_BOX_SIZE = 4


def _make_qr(text: str, box_size: int | None = None):
    """The QR for `text`, ECC level M, `QR_BOX_SIZE` px per module (`box_size` when given: the panel's PNG). python-qrcode puts a str in byte mode
    as its UTF-8 bytes, with no ECI header; ZXing (portico-app's
    QrFrameDecoder, core 3.5.3) reads a byte segment that is valid UTF-8 with
    any multi-byte character as UTF-8."""
    import qrcode

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=box_size or QR_BOX_SIZE)
    qr.add_data(text)
    qr.make(fit=True)
    return qr


def _build_qr_data_uri(payload: dict[str, Any], box_size: int | None = None) -> str:
    """Encode `payload` as JSON into a QR code, return it as a
    data: URI PNG suitable for a flow step's Markdown description. `box_size` only when it is not the dialog's:
    the dialog's call, and the tests' patches of this function, take the payload alone."""
    text = _qr_text(payload)
    image = (_make_qr(text) if box_size is None else _make_qr(text, box_size)).make_image()

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
