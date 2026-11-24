# Keys, tokens and secrets

Every key, token and password that has to be created and handed to Portico (or to the phone app), where it is made, what it may do, where it is entered, how it is rotated, and what stops working without it. Derived from the integration's code and its existing pages; items I could not trace to code are marked **unverified**.

Related: [prerequisites](../getting-started/prerequisites.md), [Tailscale](../guides/tailscale.md), [push notifications](../guides/push-notifications.md), [enrolling a phone](../guides/enrolling-phones.md), [door station](../guides/door-station-dahua.md), [AI-guided setup](../getting-started/ai-setup-prompt.md).

## At a glance

| # | Secret | You create it | Needed for | Required |
| :--- | :--- | :--- | :--- | :--- |
| 1 | SIP password | yourself (8 to 15 characters) | the door station and the SIP-server door | for `gate_sip_server` |
| 2 | Container control token (IPC token) | yourself (24 or more characters) | the SIP server in a container | container form only |
| 3 | Enrolment code | Portico makes it (QR) | enrolling one phone or screen | for each phone |
| 4 | Tailscale credential | Tailscale admin console | phones away from home | optional |
| 5 | Firebase service-account key and `google-services.json` | Firebase / Google Cloud console | waking an away phone | optional |
| 6 | UniFi Protect API key | UniFi Protect | the `unifi_protect` door | only for that driver |
| 7 | Door station RTSP user and password | the door station | the `dahua_sip` driver's video | only for that driver |
| 9 | Home Assistant access for the AI assistant | Home Assistant profile page | the [AI-guided setup](../getting-started/ai-setup-prompt.md) | only if you use an assistant |
| 10 | App signing key | the app's maintainer | building your own APK | only if you build the app |
| 11 | Publishing tokens | the maintainer | releasing wheels or APKs | maintainers only |

Phones and screens do **not** need a Home Assistant long-lived access token. Portico makes each phone its own Home Assistant user and sign-in from a one-time code (row 3). If anything asks you to paste a long-lived token into the phone app, that is not Portico's flow.

## 1. SIP password

- **What.** The shared secret the door station uses to register to the SIP server (digest authentication). It is the station's `SIP.AuthPassword`.
- **Create.** Choose it. **8 to 15 characters.** Below 8 the station sends no REGISTER at all; Portico's form and the container both refuse a value outside the range.
- **Enter.** On the station (`Network Settings > SIP Server`, see [door station](../guides/door-station-dahua.md)). In Home Assistant: the `password` field of the "SIP-server door" form. For the container form: `PORTICO_SIP_PASSWORD_FILE` (preferred, a file mounted read-only) or `PORTICO_SIP_PASSWORD`, or `GATE_SIP_PASSWORD` in a `.env` read with `-secrets-env`.
- **Where it is stored.** In the Home Assistant config entry (`.storage/core.config_entries`, file mode 0644, so it is in Home Assistant backups; the maintainer accepted this for a home install and declined to move it). Redacted in diagnostics downloads. In the container form, only in the container's secret; the Home Assistant entry holds none.
- **Scope.** Authorises registration of whoever knows it. The server also accepts an INVITE only from the registered station's address (`allow_from`), so the password alone does not let a host elsewhere ring the house.
- **Rotate.** Change it on the station and in Portico at the same time, then check the log for `REGISTER ... digest=OK` (a `digest=MISMATCH` means they differ). Plan for a few minutes without registration.
- **Without it.** No registration, no ring through the SIP server, no answering. The house may still ring through an automation on a separate door-button event, if you have one.

## 2. Container control token (IPC token)

- **What.** The shared secret between Home Assistant and a SIP-server container (the control link on its TCP port). Full control of the transport, so treat it like a password. The link is **not encrypted**: keep it on loopback, a private Docker network or a tunnel.
- **Create.** Choose 24 or more characters, no whitespace (random is best, for example `openssl rand -base64 33`).
- **Enter.** In the container: `PORTICO_IPC_TOKEN`, or a file with `-ipc-token-file`, or `GATE_SIP_PEER_TOKEN` in a `.env` read with `-secrets-env`. In Home Assistant: the token field of the "SIP-server door, remote container" form (stored in the entry, redacted in diagnostics).
- **Rotate.** Change both and reload the Home Assistant entry; the link reconnects with a back-off (1 to 30 seconds, the longest after a wrong token).
- **Without it.** The container refuses to start without a token; Home Assistant cannot connect, and the door shows unavailable.

## 3. Enrolment code (per phone or screen)

- **What.** A one-time code in a QR that lets one app obtain its own Home Assistant sign-in. 24 random bytes, good for **10 minutes and one redemption**; five wrong codes cancel that QR.
- **Create.** In the Portico panel (sidebar, administrators), **Phones** tab, **Enrol a device**. See [enrolling a phone](../guides/enrolling-phones.md).
- **Enter.** The app scans the QR. A pasted text is the fallback. The QR itself carries no Home Assistant token. It carries `ha_url`, the code, the phone name, an `enroll_id` (keep it out of logs and exports), and, depending on setup, a Tailscale key (item 4), the Firebase client values (item 5) and `ha_dial`.
- **What the phone then holds.** Its own Home Assistant user (`Portico - <name>`, not an administrator) with a refresh token (unused ones expire after 90 days) and short-lived access tokens, kept in the app's encrypted storage. What it may do is limited to its own **profile** (the pages and buttons you give it), whatever Home Assistant permissions the user has.
- **Rotate or revoke.** Revoke the phone in the Phones tab: its tokens, its user (if Portico made it), its push registration and its profile are removed. Enrol again for a new sign-in.
- **Without it.** The app cannot sign in.

## 4. Tailscale credentials

Only for phones away from home. Full steps in [Tailscale](../guides/tailscale.md); a summary:

| Mode | You create | Scope | Entered in |
| :--- | :--- | :--- | :--- |
| `auto` (recommended) | an **OAuth client** (`tskey-client-...`): Settings > Trust credentials > Credential > OAuth, scope `auth_keys` Write, bound to your device tag. Alternative: an **API access token** (`tskey-api-...`), which expires after the 1 to 90 days you choose | can mint pre-authorised, single-use, non-ephemeral keys carrying your tag | the push entry's Configure > Tailscale mode > `auto`; kept in the entry |
| `approve` | an **OAuth app** (`auth_keys:create:once` scope, your callback as redirect URI) and, once, an **admin API access token** to create it (revoke it afterwards) | each phone is approved on Tailscale's consent page; an admin account owns the phones | the OAuth app's client secret (`tskey-app-<id>-<secret>`) goes in a private `0600` store (`.storage/portico.tailscale_oauth_app.<entry_id>`); the admin token is used for one request and stored nowhere |

- **Also needed:** a device **tag** (`tag:...`) defined in the tailnet policy `tagOwners`, and an access rule letting that tag reach Home Assistant (and the camera server if you use live video away). Portico cannot write the policy.
- **Per-phone key.** Minted for each enrolment, single use, valid from 10 minutes (QR code, `auto`) to one hour (`approve`, as Tailscale fixes it). It travels in the QR. The phone redeems it right after saving and the app deletes it once its node is up, or after 55 minutes.
- **Rotate.** Replace the OAuth secret or token in Configure; the mode screen shows stars where a secret is stored, and leaving them keeps the stored one. An API token must be replaced before it expires or minting fails.
- **Without it.** Phones work at home. Away from home they cannot reach Home Assistant, and cannot answer, unless some other route (your own VPN, a public https URL) exists.

## 5. Firebase: service-account key and `google-services.json`

Only for waking a phone that is away or asleep.

- **Service-account key (secret).** A JSON key Home Assistant sends push messages with. Create a **dedicated** service account in the Google Cloud console of your Firebase project with only the **Firebase Cloud Messaging API Admin** role, then Keys > Add key > JSON. The console's default `firebase-adminsdk` key works but has broad rights. Enter it in the "Push ring notification (Firebase)" entry; Portico checks it once with Google (fetches one access token, sends nothing) and keeps it in a private store (`.storage/portico.push_credentials`, mode 0600). It is part of Home Assistant backups, so encrypt backups. **Rotate:** add a new key, upload it under the entry's Configure > Replace the service-account key, delete the old key.
- **`google-services.json` (not secret).** From the Firebase Android app you registered with package `com.meda.portico`. Portico reads four values (`project_id`, `sender_id`, `app_id`, `api_key`) and puts them in the enrolment QR, so one scan sets the app's Firebase up. **Unverified** whether Google restricts that API key to your package and its signing certificate; the maintainer's note is that the values are not secret.
- **Cloud Messaging API (V1)** must be enabled on the project.
- **Without it.** Phones ring only while their websocket is up. An away or asleep phone can miss a ring.

## 6. UniFi Protect API key

Only for the `unifi_protect` driver (video and talkback through a Protect console; ring detection is optional).

- **Create.** A key of its own for Portico in UniFi Protect's integration settings (the Protect "Integration API" key, sent as `X-API-KEY`). **Unverified:** the menu path varies by Protect version; follow UniFi's documentation for the public Integration API. Use a separate key so you can revoke it alone.
- **Enter.** The `nvr_api_key` field of the door form (`unifi_protect`), or the Protect API key field of the front-door talk options. It is never shown again.
- **Stored in.** the Home Assistant config entry (file mode 0644), like the SIP password.
- **Without it.** The driver cannot reach the console. Answering and talking need it.

## 7. Door station RTSP user and password (`dahua_sip` only)

The `dahua_sip` driver asks for `rtsp_username` and `rtsp_password` for the station's video, plus `sip_user` (`user#index`) and `sip_password`. Create a restricted user on the station for this rather than the administrator. `dahua_sip` is not activated against any real station (see [door station](../guides/door-station-dahua.md)), so this is **unverified**. For `gate_sip_server`, the station's video reaches Portico as a Home Assistant camera entity and Portico holds no camera password.

## 9. Home Assistant access for an AI assistant

The [AI-guided setup](../getting-started/ai-setup-prompt.md) works best when the assistant can read your Home Assistant. Two ways, both yours to decide:

- A **long-lived access token**: profile page of an administrator (bottom, Security) > Long-lived access tokens > Create. Shown once. It acts as that user with that user's full rights. Give it to the assistant's connection only (for example an MCP server's configuration), never paste it into a chat, and **delete it when setup is done**.
- An MCP or API connection you already run. Same rule: a read-only role where the tool offers one.

Nothing in Portico itself needs this token.

## 10. App signing key

An APK signed with another key does not install over the project's APK, and a lost key ends updates for everyone who holds an APK signed with it. Only builders need one: a PKCS12 keystore kept outside the repository, named by `PORTICO_SIGNING_PROPS`. See the app repository's README ("Release build"). Users of a released APK need no key.

## 11. Publishing tokens

Wheel releases and APK releases are uploaded with the maintainer's forge token and the signing key. These never go to a user, to Home Assistant or to a phone. A user needs no token to **download** a published wheel or APK once they are published (the wheel pin in `door/wheel.py` is read anonymously). **Unverified** until the public route exists, see [prerequisites](../getting-started/prerequisites.md#the-transport-wheel).

## What is never needed

- A long-lived Home Assistant token in the phone app (retired: enrolment format 1 carried one; it is no longer made).
- A Tailscale account on each family member's phone (phones are owned by the approving admin or tagged).
- A Google account on the phone for Portico's own sign-in.

## Handling rules

- Do not paste a secret into a chat, a ticket, a screenshot or an AI assistant's prompt. Type it into the Home Assistant form yourself.
- Home Assistant backups hold every one of these (the entries and the private stores). Encrypt them.
- Diagnostics downloads redact by rule: any field that looks secret is replaced, and addresses and names become per-download pseudonyms.
- Delete any key you created only to set something up (the Tailscale admin token, an assistant's access token).
