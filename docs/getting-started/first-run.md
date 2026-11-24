# First run: a door, a phone, a first call

The shortest path from an installed integration to a ring on a phone. Each step names the page that has the details.

Previous: [requirements and installation](install.md). Prerequisites: [prerequisites](prerequisites.md). Guided version of this whole page: [AI-guided setup](ai-setup-prompt.md).

## 1. Add the integration

Settings > Devices & services > Add integration > **Portico**. A menu appears with these entries (from `strings.json`):

| Menu entry | What it does |
| :--- | :--- |
| **Add a door** | adds one door per physical device: pick a driver, enter its address and credentials, then optionally point the relay at an HA action and at an existing camera entity |
| **Push ring notification (Firebase)** | the optional push entry; it is also the install-wide settings host (its Configure menu enrols phones and sets the Tailscale mode) |
| **SIP-server door (advanced, not live)** and **SIP-server door, remote container (advanced, not live)** | the `gate_sip_server` driver, labelled "not live" by the integration itself |

What each phone shows is edited in the **Portico** panel in the sidebar (administrators), not in Configure.

## 2. Add a door

Choose **Add a door** and a driver. The drivers, their fields and what each door can do are in [`door/README.md`](../../custom_components/portico/door/README.md) ("Drivers and adding a door"):

| Driver key | Door |
| :--- | :--- |
| `dahua_sip` | a Dahua villa station (VTO) over SIP, with RTSP video |
| `ha_event` | a ring-only door whose ring comes from a Home Assistant event entity |
| `unifi_protect` | a UniFi Protect doorbell camera: video and talkback, optional ring detection |

For a Dahua door station that registers to Portico's own SIP server (`gate_sip_server`), the station's settings, the log lines that prove it registered and the troubleshooting table are in [configuring a Dahua door station](../guides/door-station-dahua.md). Every secret you will be asked for is listed in [keys and secrets](../reference/keys-and-secrets.md).

The door side works without a phone and without the push notification: the call-state sensor updates and the `portico_intercom_call` event fires on the Home Assistant bus, so an ordinary automation can react to a ring.

## 3. Enrol a device

In the Portico panel, **Phones** tab, **Enrol a device** draws a one-time QR; the Portico app scans it. See [enrolling a phone](../guides/enrolling-phones.md). Phones away from home need a path to Home Assistant: see [Tailscale](../guides/tailscale.md); to be woken when away see [push notifications](../guides/push-notifications.md) (optional).

The Android app has to be installed first and needs a few permissions (see [prerequisites, Android phones](prerequisites.md#android-phones)). To show the door on a wall tablet and let it take over the screen on a ring, see [the wall tablet](../guides/wall-tablet.md).

## 4. Decide what the phone shows

Pages, blocks, cameras and action buttons are edited in the panel's **Pages** tab: [content, pages and actions](../guides/content-pages-and-actions.md).

## 5. The first call

Ring the door. The call state changes (`ringing`), phones ring, someone answers from the app or the dashboard card ([the card and panel](../guides/dashboard-card-and-panel.md)), and a button in the phone's profile can open the relay. Whether a particular driver supports answering and talking differs per door; see the driver table above. Nothing here has been re-run against real hardware for this documentation (unverified).
