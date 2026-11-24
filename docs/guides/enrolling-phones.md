# Enrolling a phone: the QR and the Phones tab

How a phone is enrolled: what the enrolment QR carries, and the Phones tab of the Portico panel (list, Enrol, Manage, Revoke).

Related: [one-time code and phone entries](enrolment-code-and-phone-entries.md), [Tailscale](tailscale.md), [push notifications](push-notifications.md), [prerequisites](../getting-started/prerequisites.md#android-phones), [keys and secrets](../reference/keys-and-secrets.md#3-enrolment-code-per-phone-or-screen), [wall tablet](wall-tablet.md).

## Installing the app and enrolling, in order

1. Check the phone: Android 8.0 or newer, and it can reach your Home Assistant address now (home Wi-Fi is easiest).
2. Install the Portico app from its APK. Where the public APK is downloaded from is not settled yet (see the app repository); download only from the project's own releases. Android asks once to allow "install unknown apps" for the browser or file manager you used. An APK signed with another key does not install over an existing one.
3. Open the app. It opens on Settings. Allow notifications when asked.
4. On a computer, in Home Assistant, open the **Portico** panel (sidebar, administrators) > **Phones** > **Enrol a device**. Give the phone a name. A QR appears; it is good for 10 minutes and one use.
5. In the app, **Scan the QR code** (this is where it asks for the camera, only for the scan). The app signs in and shows the connection as up.
6. Open the app's Settings and clear every problem it lists, each has a fix button: notification permission, the ring channel, the full-screen intent over the lock screen (a separate switch in Android's settings on a sideloaded install), battery guidance, the connection to Home Assistant, and, with push set up, the push registration.
7. In the Phones tab, check the phone shows as enrolled and, under **Manage**, set which doors it may open.
8. Ring the door once (with the household told) to test, on Wi-Fi first, then with the screen locked, then away from home if Tailscale is set up.

The rest of this page is the reference: what the QR carries and the panel's commands.

## The enrollment QR

Settings > Devices & Services > Portico (the push ring notification entry) > Configure > **Enroll a device** enrolls a phone and shows one QR,
compact JSON (UTF-8, non-ASCII such as a Hebrew phone name written as itself, not as `\u` escapes):
`{"portico": 2, "ha_url", "code", "phone_name", "enroll_id"}` plus `tailscale_authkey` when Portico mints
Tailscale keys, plus `fcm` when there is a push ring notification entry, plus `ha_dial` when there is a valid
one (see "What else the QR carries" at the end of this page). `portico` is the format marker; an app that does not know the number refuses the QR with "update the
app" and saves nothing. **The QR carries no Home Assistant token** (DEV-544): `code` is a one-time code, redeemed
for the phone's own sign-in ([the one-time code](enrolment-code-and-phone-entries.md)). Format 1, which carried a ten-year token as `ha_token`, is not made any
more and there is no setting that brings it back. `enroll_id` is a random,
id (`secrets.token_urlsafe(32)`; it is in the QR, but it is also the only credential behind offline
registration, so keep it out of logs and exports; diagnostics redact it), also stored in the phone's
entry ([phone entries](enrolment-code-and-phone-entries.md)); it lets a pasted registration string be matched to the phone ([push notifications](push-notifications.md), [phone entries](enrolment-code-and-phone-entries.md)). An app that does not know it ignores it.

## The Phones part of the Devices tab

The Portico panel (sidebar, administrators only) has a **Phones** tab: the list of phones, **Enrol a device** and, while a
QR is on screen, **Cancel** and **New code**. Each row has **Manage** (UI split P3): the doors the phone may open (all, or
a ticked list of the doors Portico has now, from `portico/doors`), its push ring notification (all of its push registrations), its
house-call switch, a **Preview its screen** link (the existing content preview, shown only when the content document has
exactly one phone of that name) and **Revoke this phone**, which asks on a confirmation page that lists what it destroys
(the server refuses a revoke without `confirm: true`, whatever the page did). The list also shows a read-only gate
status line (`sensor.call_state`, `sensor.listen_leg` from `hass.states`) when those sensors exist. The dialog above stays
as the recovery path. The tab is its own module,
`www/portico-phones.js` (served and content-hashed like the panel; the panel imports it lazily, and when the import
fails the tab is simply absent and the content tabs still work). It writes immediately and has no draft, so the content
header (Undo, Discard, Save) is hidden on it. The tab bar is shown in the panel's error and needs-restart phases too,
so Phones is reachable when the content store is broken.

## Phone websocket commands and the QR as an image

Five websocket commands, all `admin` in `auth.COMMANDS` (the panel is open to every household login, so the class is the
only access control), all in `phones_commands.py`:

| Command | Does |
| :--- | :--- |
| `portico/phones/list` | `{header, phones}`. The header: whether the push entry exists, the saved Tailscale mode and whether its credentials are complete, `enrol_here` (the page can enrol: `manual`, or `auto` with credentials), the remembered `ha_dial`, how many QR codes are waiting and the cap. Rows (legacy `enrolled_phones` rows included, nothing written or adopted): `entry_id` (the phone's config entry; a legacy row gets a hash of its own key; **never the `enroll_id`**), `name`, `state`, `expired` (a `pending` phone whose code was lost, for instance at a restart), `code_expires_in`, `enrolled_at`, `last_connection`, `push_registered`, `doors`, `away_ring`, `house_receive`. |
| `portico/phones/enroll {name, allow_http, ha_dial, confirmed_entry_id?}` | Draws a one-time QR through the enrolment core (`enrollment_core.create_enrollment`, the same function the dialog calls). Replies `{result: "created", entry_id, qr_token, modules, expires_in, expires_at, notes, dial_target, created}` or `{result: "needs_confirm", entry_id, name}` (a result, not an error: an enrolled phone of that name exists; send again with `confirmed_entry_id`), or an error with one of `needs_tailscale_setup`, `needs_approval`, `no_ha_url`, `no_https_url`, `invalid_ha_dial` (the frame's `error.field` names the field), `tailscale_mint_failed`, `no_push_entry`, `bad_name`, `pending_cap`, `unknown`. A blank `ha_dial` forgets the remembered one. |
| `portico/phones/cancel {entry_id}` | Kills the code of a QR this server drew. The phone entry is removed too **only** when this server remembers (in memory, next to the QR token) that this enrolment created it and it is still `pending` with a live code; a phone that has enrolled, or one this server did not just draw, is never removed here. A flag from the client is never read (the schema takes the entry id alone). Replies `{result: "cancelled", removed}` or `unknown_phone`. |
| `portico/phones/revoke {entry_id, confirm}` | UI split P3. Revokes one phone for good: removes its entry (a legacy `enrolled_phones` row is taken off its host entry), which revokes its HA tokens, deletes its HA user **only** when it is a user Portico made (`Portico — <name>`, not an administrator, not the owner, not a system user; any other user is kept and loses only the tokens enrolment made), its push registrations, its content override, a pending code and its QR. **`confirm` must be exactly `true`; the server answers `confirmation_required` without it** (the panel's confirmation page is not the only guard). The user deleted is the one named in that phone's own row. Replies `{result: "revoked", legacy}` or `unknown_phone`. Portico does not delete the phone's Tailscale node (a notice names it). |
| `portico/phones/set {entry_id, patch}` | UI split P3. `patch` is a non-empty object of `doors` (`null` = every door, or a list of door ids, each one `portico/doors` lists now, otherwise `unknown_door` with `field: "doors"`; the list may be empty = no door, as the dialog allows), `away_ring` (bool: on or off for every push registration the phone has; `no_registration` when the app has not registered) and `house_receive` (bool, stored in the phone entry's options; it takes effect only while house calls are switched on: the House calls section of the Devices tab, `portico/house_calls/set`). A legacy row accepts only `away_ring` (`legacy_phone` otherwise). The whole patch is checked before anything is written. Replies `{result: "updated", phone: <the row as `list` sends it>}`. |

**The QR is never a websocket frame** (HA logs every frame at DEBUG, and the QR decodes to the one-time code and, in `auto`
mode, a pre-authorised Tailscale key). `enroll` replies with `qr_token` (32 random bytes, kept in memory only, as a digest)
and `modules`; the panel fetches `GET /api/portico/enroll/qr/<qr_token>` once, as a blob, with the user's own bearer header
(`hass.fetchWithAuth`, or `hass.auth`'s access token refreshed when expired), and draws it from an object URL. The view
(`enroll_view.py`) is administrators only: **`403` for a non-administrator, `404` for an unknown, cancelled, expired,
redeemed or superseded token, never `401`**, because a 401 from a view feeds HA's IP-ban counter. (HA's own
`require_admin` decorator is not used: it raises `Unauthorized`, which HA turns into exactly that 401.) A request with no
login at all is answered `401` by HA before the view runs. `Cache-Control: no-store`; the token is not consumed by a read
(a retry after a network blip works) and stops working with its code.

**Copy enrolment text (for a tablet that cannot scan).** Beside the QR the page has a **Copy enrolment text** button. It
fetches `GET /api/portico/enroll/text/<qr_token>` (`enroll_view.py`, `text/plain`) with the user's own bearer header, the same
way as the PNG, and puts the text on the clipboard (`navigator.clipboard.writeText`); where the browser refuses (a plain-http
page, no permission) the page shows a read-only, select-all text field instead. The text is **exactly what the QR encodes**
(compact JSON, `enrollment_flow._qr_text`), so the app's Settings enrolment (paste) field takes it as it takes a scanned QR;
a test rebuilds the PNG from the served text and compares bytes. The rules are the PNG's: administrators only (`403`, never
`401`), `404` for an unknown, cancelled, expired, redeemed or superseded token, `Cache-Control: no-store`, the same lifetime
(the store keeps the text beside the PNG and drops both together). The text is never a websocket frame, is not fetched until
the button is pressed, is kept in memory for the page only, and is never logged or put in a `repr`. It holds the one-time
code (valid 10 minutes, one use) and, in `auto` mode, the Tailscale key: **treat it like a password**; it holds no Home Assistant
token. Use case: enrolling a wall tablet (paste with `adb shell input text`, or type or paste it on the tablet).

The PNG is one pixel per module with the 4-module quiet zone. The panel draws it at an **integer number of device pixels
per module**, `k = clamp(floor(available_device_px / (modules + 8)), 3, 6)`, CSS width `(modules + 8) * k / devicePixelRatio`,
`image-rendering: pixelated`, on white (never inverted by a dark theme), re-evaluated on resize and zoom; below `k = 3` it
draws nothing and says to scan from another screen. **A longer name makes a denser code**: the realistic payload is 477 bytes,
version 17, 85 modules; a 40-character name made it 505 bytes, version 18, 89 modules, and ZXing 3.5.3 (the app's
decoder) then often did not find the code at any scale. **A phone name is therefore capped at 24 characters** (the dialog
and the panel alike; decided 2026-10-23): a 24-character ASCII name is 489 bytes, still version 17, and read 20 of 20 times
(`scripts/qr_decode_check.sh` reproduces it). The panel still says so when a code has 89 modules or more (a Hebrew name,
a long address or key).

`scripts/qr_decode_check.sh` decodes the real QR (the command, the core and the view; nothing patched) with ZXing in a JDK,
offline, and redeems the code it read. It is a documented one-off (no QR decoder is in the test venv).

## What else the QR carries: ha_url, ha_dial, fcm

`ha_url` (Settings > System > Network): the external URL if it is https, else the internal URL if
it is https. Plain http is used only when the installer ticks **Allow plain http on my own
network** on the enrollment form (off by default; the connection and the phone's token are then
not encrypted). With only http URLs and the box off, the flow refuses with `no_https_url`; with no
URL at all it refuses with `no_ha_url`. Either way nothing is minted.

`ha_dial` (for example `"ha.example.net:443"`) is where the phone dials Home Assistant through its own
Tailscale node when `ha_url` does not answer, away from home. It must be an IP address and a port:
the app's embedded Tailscale (tsnet) looks names up in MagicDNS and then in public DNS, never in
the tailnet's split DNS, so an internal name such as `ha_url`'s host cannot be dialled. The
enrollment form's optional **Address phones use over Tailscale** sets it; left blank it is
automatic: Home Assistant's own IPv4 address on its default network adapter (Settings > System >
Network), when that adapter is enabled and has exactly one private address, joined with `ha_url`'s
port (its explicit port, else 443 for https and 80 for http). Type one when that is not where the
phone should connect, for example when `ha_url` points at a reverse proxy on another machine. The
value follows portico-app's own rule (`ha_dial.py`, a port of `TailnetTarget.parseHaDial`): a
canonical IPv4 in 10/8, 172.16/12, 192.168/16 or 100.64/10, or `[IPv6]` in fc00::/7, and a port
1-65535; no host names, loopback, link-local or public addresses. The app refuses the whole
enrollment over an invalid one, so a typed value it would refuse is refused on the form
(`invalid_ha_dial`) and nothing is created; when automatic detection finds nothing usable the key
is left out and the app dials `ha_url`'s own host. A typed value is not secret and is suggested
again on the next enrollment (entry data `ha_dial`); a blank field forgets it. The QR page names
the address the phone will dial. **Prerequisite:** the tailnet must let the phone's node reach that
address and port, typically a subnet router advertising the LAN (approved in the admin console)
and an ACL rule that allows the phones' tag or owner to reach it; without one the phone joins the
tailnet and still cannot connect.

`fcm` is `{"v": 1, "project_id", "sender_id", "app_id", "api_key"}`: the four Firebase client
values from the push ring notification entry's `google-services.json`, which are not secret. Nothing of the
service account is ever in the QR. With no push ring notification entry, or one set up without
`google-services.json`, the key is absent and the app keeps the Firebase settings it has. If the
stored values are ones the app would refuse (portico-app refuses the whole enrollment over a bad
`fcm` block), the block is left out and the QR step says so.
