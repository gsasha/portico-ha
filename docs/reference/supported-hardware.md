# Supported hardware and assumptions

What Portico is built for and tested with, what exists in the code but has not been tried on real hardware, and the minimum versions. Written from the code and the maintainer's records. "Tested" here means the maintainer ran it on their own installation; nothing in this repository is certified by a vendor.

Related: [prerequisites](../getting-started/prerequisites.md), [door station configuration](../guides/door-station-dahua.md), [keys and secrets](keys-and-secrets.md), [AI-guided setup](../getting-started/ai-setup-prompt.md).

## At a glance

| Hardware or service | Role | Status |
| :--- | :--- | :--- |
| **Dahua villa door station `DHI-VTO3222E-P`**, firmware `4.810.0000000.1.R` (build 2025-09-19), 2-wire | door station that registers to Portico's SIP server | **Tested**: one unit. LAN calls with two-way audio worked in a supervised trial window (2026-11-18) against Portico's SIP server in its container form. |
| Other Dahua villa stations (VTO family) | same | **Untested.** Menu names, config leaves and firmware behaviour may differ. The open-source `dahuadoor` binding for openHAB uses the same "station registers to an external SIP server" arrangement, so the arrangement is not unique to this unit. |
| **Portico's own SIP server** (`gate_sip_server` driver, in the transport) | the SIP registrar and answerer the station talks to | In the code and tested against recorded and synthetic exchanges; run against the real station once, in a container. The form that runs it inside Home Assistant has not yet been run against the real station (as of 2026-11-18). |
| `dahua_sip` driver (Portico registers to the station like a phone) | Dahua station as a SIP server | In the code. **Not usable** on a station with `Local SIP Server` off, which is the working setup (first real start timed out); not activated against any station. |
| **IP cameras** with a Home Assistant `camera` entity | tiles with still pictures | Any camera Home Assistant can take a picture from. Stills are fetched through Home Assistant's camera support. |
| **Frigate** with its go2rtc, or **Home Assistant's built-in go2rtc** | live video on tiles; the RTSP route for phones away from home | Used on the maintainer's installation. Frigate integration entities (with `camera_name` and `client_id` attributes) are what the away route needs. |
| **UniFi Protect doorbell** (`unifi_protect` driver) | video and talkback through the Protect console's API; optional ring detection | In the code and unit-tested. **Ring detection is not verified against a real console**, nor whether a given doorbell model emits `ring` on the public events stream. Home Assistant's own UniFi Protect integration is the source for a ring-only `ha_event` door instead. |
| **Any Home Assistant `event` entity** that fires a ring (`ha_event` driver) | a ring-only door: no audio, no relay | Used on the maintainer's installation for a UniFi Protect front-door doorbell (event type `ring`). |
| **Android phones** with the Portico app | ring, answer, buttons, cameras | Android 8.0 (API 26) or newer. **Tested** on a small number of the maintainer's own phones. iOS: **not supported**; no iOS app exists. |
| **Wall tablets / kiosk screens** | ring takeover and idle page | A tablet in a kiosk browser (Fully Kiosk on Android was used). Idle page seen on a real tablet; **the ring takeover and tablet-side answering were not yet proven with a real ring** at the last record (2026-10-24). |
| **Home Assistant OS on x86_64** | the host | **Tested.** `aarch64` has a wheel; **untested** on real hardware by the maintainer (the pinned wheel is built for it). Container, Supervised and Core installs: **unverified**. |
| **Tailscale** | remote calling | Used on the maintainer's installation; optional. |
| **Firebase Cloud Messaging** | waking a phone that is away | Used on the maintainer's installation; optional; you bring your own Firebase project. |

## Minimum versions

| Component | Version | Source |
| :--- | :--- | :--- |
| Home Assistant | **2026.10.2**: the one version the tests run against; older and newer are **untested** | [running the tests](../development/running-tests.md) |
| Integration `portico` | 0.3.0, alpha | `custom_components/portico/manifest.json` |
| `portico_transport` wheel | pinned **0.7.1**; the integration refuses to attach a door below **0.5.0** (`MIN_WHEEL_VERSION`). Some features need more: house calls need 0.6.0, the `gate_sip_server` driver 0.6.4, the front-door talk driver 0.6.5 | `door/wheel.py`, `door/hub.py` |
| Wheel platforms | `x86_64`, `aarch64`. No 32-bit ARM. | `door/wheel.py` |
| Portico app | contract 3: **0.5.0-alpha or newer**; push registration from 0.5.0; the server answers an older app's registration with `app_too_old`. Android 8.0 (API 26) or newer | [websocket auth](websocket-auth.md), app repository |
| Python package | `qrcode>=7.4` (Home Assistant installs it) | manifest |
| Door station firmware | `4.810.0000000.1.R` is the only one with notes | [door station](../guides/door-station-dahua.md) |
| Tailscale | the app embeds its own client; Home Assistant needs the Tailscale add-on or any client; no version has been pinned | [Tailscale](../guides/tailscale.md) |

## Assumptions the code makes

- The door station is a SIP **client** registering to a server whose address you give it, with `Local SIP Server` **off**. Portico's server expects: realm `VDP`, user `8001` by default (the station's `SIP.UserID`), digest authentication, REGISTER about every 5 minutes, one call at a time.
- Audio is 16 kHz 16-bit little-endian PCM (`PCM/16000`) when the station offers it, else G.711 A-law (`PCMA/8000`), 40 ms packets. µ-law is not offered by the SIP server.
- One station has one registrar. Two SIP servers for one station are not supported.
- Opening a door or gate is a Home Assistant action (a `button`, `lock`, `cover` or `script` entity), not a SIP message.
- The door is reachable from the machine that runs the transport by IP address (the SIP server forms take IP literals, not names).
- Phones reach Home Assistant at an https URL, or at a private address over http, or through the tailnet. See [prerequisites](../getting-started/prerequisites.md).
- Home Assistant has an administrator for the panel; every phone and screen is a separate non-administrator user created by Portico or by you.

## What is not supported or not tried

- Doors other than the drivers above (for example generic SIP intercoms such as Doorbird, 2N, Hikvision, Akuvox, or Axis): **untested**. A generic SIP door could work through the SIP server if it can register to an external server and speak PCMA or `PCM/16000`; no one has tried.
- ONVIF or vendor push events as ring sources: not implemented. Use `ha_event` with any Home Assistant event entity.
- iOS, Wear OS, Apple CarPlay, Android Auto: not supported.
- A doorbell that only switches a chime relay with no data connection: nothing for Portico to talk to.
- Calls between phones (house calls) exist in the code and are off by default; not described as supported.

If you try other hardware, the [connection log](../guides/connection-log.md) and the transport log are the first evidence to collect.
