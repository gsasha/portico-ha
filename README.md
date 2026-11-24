# Portico for Home Assistant

Portico turns Home Assistant into the door-and-intercom system of a house. A visitor rings the gate or the front door, Home Assistant knows about it, the phones in the house (and phones that are away) ring, someone answers from the Portico phone app or from a dashboard, and the door can be opened.

This repository holds the Home Assistant side: one custom integration (domain `portico`) plus the small transport process it bundles for the real-time parts (SIP, RTP, WebRTC). The Android phone app lives in a companion repository, **portico-app** (not part of this repository).

> **Status: alpha.** The integration is at version 0.3.0 (`custom_components/portico/manifest.json`) and has not been published through HACS. It was built for one household and is being prepared for others, so expect rough edges and some documents that still name that household's own setup (they are marked). Nothing in this README has been re-run against real hardware while writing it; statements about hardware are taken from the code and the existing documents.

## What it does

- **Door and gate calls.** Doors are config entries with a driver: `gate_sip_server` (Portico's own SIP server, which the door station registers to), `dahua_sip` (a Dahua villa station over SIP, with RTSP video), `unifi_protect` (a UniFi Protect doorbell camera) and `ha_event` (a ring-only door fed by a Home Assistant `event` entity).
- **Call state in Home Assistant.** A call-state sensor and a `portico_intercom_call` event on the HA bus, so an ordinary automation can react to a ring, with or without any phone.
- **Phone enrolment.** An administrator enrols a phone from the Portico panel with a one-time QR code; the phone gets its own Home Assistant user and sign-in (the QR carries no token).
- **Ringing phones.** Phones ring over Home Assistant's websocket. An optional Firebase push notification wakes a phone that is away from home; optional Tailscale support helps phones reach Home Assistant from outside.
- **Content and pages.** An admin panel in the sidebar edits what each phone or wall screen shows: pages, blocks, camera tiles (stills, optional live video) and action buttons (open a door or lock, run a script, call someone, talk to the door).
- **A dashboard card.** The `portico-card` Lovelace card shows a phone's or a screen's pages and can take over a wall tablet when the door rings.
- **House calls, Talk to the door, away call audio.** Present in the code and described in the guides; several of them are switched off by default.

## Requirements

- Home Assistant. The tests run against 2026.10.2, the one version this code is tested with.
- Python packages are installed by Home Assistant itself (`qrcode`); the transport is a pip wheel (`portico_transport`) the integration installs the first time a door starts.
- A door to talk to: see [supported hardware and assumptions](docs/reference/supported-hardware.md), and [prerequisites](docs/getting-started/prerequisites.md) for the network, ports, HTTPS and Tailscale. Dahua VTO over SIP is the documented station; UniFi Protect, Tailscale and Firebase push are optional.

## Installation

Details: [requirements and installation](docs/getting-started/install.md).

**HACS:** not available yet. The repository carries a `hacs.json` but is not published, and the wheel pin still blocks outside installs; what publishing needs is recorded in [docs/development/publishing.md](docs/development/publishing.md).

**Manual:**

```
cp -r custom_components/portico <your HA config>/custom_components/
```

Restart Home Assistant. The first door that starts installs the transport wheel.

> The wheel is pinned by URL in `custom_components/portico/door/wheel.py`. The default location is the GitHub release page of this repository; the `wheel_base_url` option (see [Deployment options](#deployment-options); set in the Portico settings entry's **Configure** > **On/off and ring window**) fetches the same files from another web root. Until the release is published there, set that option.

## First setup

1. Settings > Devices & services > Add integration > **Portico** > **Add a door**: choose a driver and give its address and credentials.
2. In the sidebar, open **Portico** (administrators): the Phones tab enrols a phone with a QR the Portico app scans; the Pages tab sets what each phone shows.
3. Optional: the **Push ring notification (Firebase)** entry from the same menu, and Tailscale for phones away from home.

The full walkthrough is [first run](docs/getting-started/first-run.md). To set up a Dahua door station, see [door-station-dahua](docs/guides/door-station-dahua.md); every key and secret you will be asked for is in [keys and secrets](docs/reference/keys-and-secrets.md). To be guided through all of it by an AI assistant, use the [AI-guided setup prompt](docs/getting-started/ai-setup-prompt.md).

## Deployment options

Nothing about one particular network is built into the code. These config-entry options carry the values that differ per installation; each is empty or unset by default.

| Option | Entry | Meaning |
|---|---|---|
| `protect_host` | the UniFi Protect door (`ha_event` driver), set in its Configure form | Address of the Protect console. Required with talk-back. |
| `camera_id` | same | The doorbell camera's id in Protect. Required with talk-back. |
| `downlink_url` | same | RTSP URL of the doorbell's audio, for example `rtsp://192.168.2.10:8554/doorbell`. Required with talk-back. |
| `guard_url` | same | `/health` URL of an older intercom relay that must be idle before talk-back starts. Empty means no external guard. |
| `wheel_base_url` | the Portico settings (push) entry: Configure > On/off and ring window | Web root the transport wheel is fetched from, for example `https://git.example.com/owner/portico-ha`. The path `/releases/download/<tag>/<file>#sha256=...` and the hash stay as pinned. Unset means `https://github.com/gsasha/portico-ha`. |
| `kiosk_background_url` | same form | Picture shown behind the kiosk page (`/portico/kiosk/<screen>`): an `http(s)` URL or a path on the Home Assistant origin. Unset means a plain black page. |
| `kiosk_back_path` | same form | In-app route the kiosk card's back rail goes to, for example `/lovelace/home`. Unset means the rail goes back in history, or closes. A Lovelace card sets the same thing with its own `back_path` key. |

The three Portico settings options are optional text fields in that form: a blank field removes the option, and an invalid value is refused with a message. Talk-back stays off until its key, `protect_host`, `camera_id` and `downlink_url` are all set. The `kiosk_*` values are sent to any logged-in user's kiosk page, so put no secret in them. `scripts/build_test_venv.sh` honours `WHEEL_BASE_URL` (same meaning as `wheel_base_url`) when it fetches the wheel for the offline test venv.

## Usage

Once a door and a phone are set up, a ring changes the call state, rings the enrolled phones and any wall screen with a card in takeover mode, and a visitor can be answered from the app or the card, depending on what the driver supports. Buttons in a phone's profile open a door, a lock or a gate cover, press a button, run a script or scene, or place a call. The profile is the grant: a phone can only run what is in its own profile, whatever Home Assistant permissions its user has.

Guides, one task per page, are indexed in [docs/README.md](docs/README.md).

## Documentation

| Where | What |
| :--- | :--- |
| [docs/README.md](docs/README.md) | the index and reading order |
| [docs/getting-started/](docs/getting-started/prerequisites.md) | prerequisites, install, first run, [AI-guided setup prompt](docs/getting-started/ai-setup-prompt.md) |
| [docs/guides/](docs/guides/enrolling-phones.md) | door station (Dahua), enrolment, Tailscale, push, wall tablet, content, live video, the card, connection log |
| [docs/architecture/](docs/architecture/overview.md) | how the parts fit |
| [docs/reference/](docs/reference/supported-hardware.md) | supported hardware, keys and secrets, websocket commands and authorisation, events, contract fixtures, versions |
| [docs/development/](docs/development/running-tests.md) | repository layout, tests, publishing |
| [`custom_components/portico/README.md`](custom_components/portico/README.md), [`door/README.md`](custom_components/portico/door/README.md) | the integration and its door package |

## Tests

Offline, in a throwaway Docker container:

```
scripts/build_test_venv.sh portico-py314-venv-2610-2 2026.10.2   # once
scripts/test_offline.sh custom_components/portico
scripts/test_offline.sh --node                                    # card and panel
```

See [running the tests](docs/development/running-tests.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): branch, run the tests, open a pull request.

## Related repository

**portico-app**, the Android app, speaks "contract 3" with this integration ([contract fixtures](docs/reference/contract-fixtures.md)). Its location is not recorded in this repository.

## License

MIT, see [LICENSE](LICENSE).
