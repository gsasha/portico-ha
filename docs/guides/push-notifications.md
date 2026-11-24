# Push ring notification: install and first run

How to set up the optional Firebase push notification that wakes a phone that is away from home, and a staged first test.

Related: [Tailscale](tailscale.md), [enrolling a phone](enrolling-phones.md).

## Push ring notification: install and first-run checklist

The push ring notification is optional. Phones at home ring over the tailnet without it; it adds a Firebase
Cloud Messaging (FCM) push that wakes a Portico phone that is away from home. Without a push entry
nothing push-related loads.

### 1. Firebase, once (about five minutes)

1. In the [Firebase console](https://console.firebase.google.com/), create a project. Google
   Analytics can stay off.
2. Add an **Android app** with the package name `com.meda.portico`. Portico does not use the
   SHA-1 field, so leave it empty unless another Firebase feature you use asks for it. Download the
   `google-services.json` the console offers.
3. Make sure the **Firebase Cloud Messaging API (V1)** is enabled for the project (Project settings,
   Cloud Messaging). If it is off, the key check in step 5 fails with a readable error, so you will
   know.
4. Make a **service-account key** for Home Assistant to send with. There are two ways:
   - **Recommended: a dedicated account.** In the Google Cloud console for the same project, open
     IAM & Admin, Service accounts, and create one (for example `portico-fcm-sender`). Give it only
     the **Firebase Cloud Messaging API Admin** role, then open Keys, Add key, JSON. If a backup
     leaks, that key can ring phones but cannot administer the project.
   - **Quick path: the console's default key.** In the Firebase console, open Project settings,
     Service accounts, *Generate new private key*. It works, but the default `firebase-adminsdk`
     account has broad rights over the project.
5. In Home Assistant: Settings, Devices & services, Add integration, **Portico**, then choose the
   driver **Push ring notification (Firebase push notifications)**. Upload (or paste) the service-account JSON,
   and optionally `google-services.json`. On the same form, set **New phones get the push ring notification**
   (see section 3).

   Portico checks the key once with Google: it fetches one access token and sends no message. It then
   stores the key only in Home Assistant's private storage (`.storage/portico.push_credentials`,
   file mode 0600), never in the integration's settings, and never shows it again. The key is still
   part of HA backups, so keep backups encrypted. To rotate the key, add a new key in the console,
   upload it under the push ring notification entry's Configure, *Replace the service-account key*, and then
   delete the old key in the console.

### 2. Phones

- Each phone runs the Portico app, enrolled with the QR from a Portico device's Configure. When the
  push ring notification entry has the `google-services.json` values, the QR carries them (its `fcm` block), so
  one scan sets up the push ring notification too. Otherwise paste the same `google-services.json` into the
  app's Setup (its "Firebase" field). A phone enrolled before the push ring notification was set up gets the
  values by enrolling again.
- The app **registers itself** over the WebSocket each time it connects. There is nothing to type
  in Home Assistant. A phone appears under the push ring notification entry's Configure, **Phones**.
- **Phones** is a multi-select. "Send pushes to" is each phone's push ring notification switch, and "Remove"
  deletes its registration. A phone that is switched off never gets a push for a real call.
  Deleting a phone's HA user also deletes its registrations.
- The diagnostic sensor `sensor.portico_push_ring_notification` shows `ok`, `degraded`, `credentials_invalid`
  or `disabled`, and per phone its state, switch and last result. It never shows a registration
  value or a key.
  It was `sensor.portico_away_ring` before 2026-11-12 (the "away ring" is now the "push ring notification"); setup renames
  an old install's sensor and entry title once (`push_ring_rename.py`), keeping the unique id `<entry_id>_away_ring` and any id the
  user renamed. The wire keys `away_ring` (phones list/set, ui_settings) and `away_ring_enabled` keep their names.

### If the phone cannot reach HA

Right after the scan the phone registers itself with Home Assistant, so it has to reach this Home
Assistant at that moment (for example on the home Wi-Fi). When it cannot, the app shows a
**registration string** instead: one block of letters and digits, with no password, token or key
in it. Copy it to wherever you run Home Assistant (a message to yourself is fine; added spaces and
line breaks do not matter) and paste it under Settings > Devices & Services > Portico > the
**Portico push ring notification** entry > Configure > **Add a phone's registration**. Home Assistant then
registers that phone exactly as the phone's own connection would: the same checks, the same row
(a later connection from the phone updates it, never duplicates it), the same "New phones get
the push ring notification" default and test-phone list. The confirmation names the phone and says whether its
push ring notification is on.

A string is refused, with the reason, when it is damaged or truncated (copy it again), comes from
a newer app, was made for another Firebase project, or matches no phone enrolled on this Home
Assistant (enrolled elsewhere, from an old QR, or its user was deleted: enroll the phone again).
When the phone can reach Home Assistant none of this is needed. The format is
`reg_string_spec.md` v1: base64url of `[0x01][CRC-32][compact JSON]`.

**Decision (owner, 2026-10-20): registration strings are not signed.** The string is accepted for
whichever phone its `enroll_id` matches; there is no per-phone signing secret. The review (HA-L10)
suggested one and it was declined: *redaction is enough*. `enroll_id` is not a secret on the wire
(the QR carries it), but it is the only credential behind this path, so diagnostics redact it
(`TO_REDACT`). Residual risk, accepted: anyone holding a support bundle made before that redaction,
or the `core.config_entries` file (mode 0644), could forge a string for an enrolled phone. The gate
is the admin who pastes it, and what a forged string can do is small: it re-points that phone's
rings to another FCM registration, and the pushes are data-only (door reference, call id, timing),
with no HA token, Tailscale key or URL in them. Reopen this if a registration string ever carries
more than that, if the paste step stops being an admin action, or if `enroll_id` is shown anywhere
else (logs, a card, an unredacted export). Do not re-propose signing without one of those.

`enroll_id` is also the key of `overrides` and `endpoints` in the content document, `.storage/portico.content`, which
is a non-private (mode 0644) Store like `core.config_entries` (recorded 2026-10-24, DEV-700). It is the same accepted
residual, and the diagnostics download pseudonymises those keys. The reopen conditions above apply to that file too.

**Known residual exposure (decision, owner, 2026-10-24, DEV-700).** The door stations' passwords (`sip_password`,
`rtsp_password`, `nvr_api_key`) and the push entry's `tailscale_api_key` stay in `core.config_entries` (mode 0644):
a home install, nothing urgent. Moving them into private Stores was considered and declined for now. The diagnostics
download is redacted by rule, not by a list of names: a key that looks like a secret (`password`, `token`, `key`,
`auth`, `secret`, `credential`, `rtsp`, `user`...) is replaced, hosts, addresses and user and phone names become
per-download pseudonyms, any URL carrying credentials is replaced whatever its key, and
`tests/test_diagnostics_redaction.py` fails when a config-flow field that looks secret is not covered. What remains
exposed is the file itself, to anything that can read `/config/.storage`, and Home Assistant backups, which hold
every Store. If that matters, encrypt the Home Assistant backups.

### 3. "New phones get the push ring notification"

An option on the push ring notification entry: on the first form, and later under Configure, *On/off and ring
window*.

- **On** (the default for a new install): a phone that registers for the first time is switched on
  straight away.
- **Off**: a new phone gets no push until you switch it on under Phones. Use this when a phone also
  runs another intercom app that rings by itself, so it does not ring twice.
- Changing the option never changes a phone that has already registered. It keeps its own
  switch, including after the app re-registers with a new registration value.
- A push ring notification entry created before this option existed behaves as **Off** until you save the
  setting. Off was the old fixed behaviour, so upgrading switches no phone on.

### 4. First run: a staged test on one phone

Prove the push on one phone before relying on it. A Google `200` only means Google accepted the
message, not that the phone got it.

1. Under Configure, **Test phones**, pick the one test phone. The list is empty by default.
2. While the Test phones list is not empty, a real door ring is pushed **only** to listed phones
   whose push ring notification switch is on, so a test cannot ring everyone. If a listed phone has since
   gone (for example, a reinstall registered it again under a new id), real rings stay restricted
   to the list until you save the list again.
3. Run the action **`portico.push_test`** (admin only), for example from Developer tools, Actions.
   It sends **one** quiet test notification ("Portico push ring notification works"), never a ring, to each
   listed phone. It ignores the phone's push ring notification switch and refuses any phone that is not on
   the list. The optional `phones` field narrows it to some of the listed phones. It returns
   `{"sent": n, "results": [{"phone": name, "result": "ok" | "dead" | ...}]}`. It refuses with a
   clear error, and sends nothing, when the list is empty, when no listed phone has a current
   registration, or when called more than once in 10 seconds or more than 10 times in an hour.
4. Then a real ring, with the test phone idle and its screen off for at least ten minutes. A phone
   that is awake hides a delivery-priority problem. Then answer elsewhere and check that the test
   phone stops ringing.
5. **Clear Test phones** when you are done, so real rings go to every phone that is switched on
   again.

### Logging: a warning about HA's own WebSocket debug log

Portico never logs a registration value, a key, an access token or an HA token. **Home Assistant's
own WebSocket API**
(`homeassistant.components.websocket_api`) logs every message a client sends when that logger is
at DEBUG. That includes the phone's `portico/push/register` message, with its registration
value, and the auth message, with the phone's HA access token. This is HA core's logging and
Portico cannot prevent it. It is off by default. If you turn on debug logging for troubleshooting,
keep that logger out of it (or turn it off again soon), and treat the log file as sensitive. A
registration value alone cannot send a push (that needs your service-account key), but the HA token
is a real credential: if one ends up in a shared log, delete that phone's HA user and enroll the
phone again.
