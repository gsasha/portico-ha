# Prerequisites

Everything that has to be in place before Portico can ring a phone and carry a call: Home Assistant, the install route, the transport wheel, the network, HTTPS, Tailscale for calls from away, Frigate or go2rtc for live video, and push notifications. Each section says whether it is required.

Next: [requirements and installation](install.md). Related: [keys and secrets](../reference/keys-and-secrets.md), [supported hardware](../reference/supported-hardware.md), [AI-guided setup](ai-setup-prompt.md).

Examples use made-up addresses (`192.168.2.x`, `ha.example.net`). Replace them with yours. Real LAN addresses are private ranges (`10.x`, `172.16-31.x`, `192.168.x`), and some Portico forms accept only those.

## Summary

| Item | Required | Why |
| :--- | :--- | :--- |
| Home Assistant 2026.10.2 (the tested version), with an administrator login | yes | Portico is a Home Assistant integration. Only 2026.10.2 is tested. |
| A way to install a custom integration (manual copy; HACS is not available yet) | yes | see [install](install.md) |
| The `portico_transport` wheel reachable from Home Assistant, once | yes, for any door | the SIP, RTP and WebRTC process |
| A door station or other ring source | yes, to get a ring | [supported hardware](../reference/supported-hardware.md) |
| An https URL for Home Assistant | yes for phones outside the LAN; strongly advised everywhere | the app refuses plain `http` outside private ranges |
| Network paths and ports (below) | yes | SIP, RTP, websocket |
| An Android phone with the Portico app | for phone ringing | [enrolling a phone](../guides/enrolling-phones.md) |
| Tailscale | for calls from outside the home | [Tailscale](../guides/tailscale.md), steps below |
| Firebase project | optional | wakes a phone that is away or asleep, [push](../guides/push-notifications.md) |
| Frigate and go2rtc | optional | live video away from home and in tiles |

## Home Assistant

- **Version.** The test suite runs against **2026.10.2**. No other version has been tested. A newer or older core may work or may not; **unverified**.
- **Installation type.** Developed and run on Home Assistant OS on an x86_64 machine. Container, Supervised and Core installs are **unverified**; the integration needs a writable config directory and the ability to install a Python package with `uv`/`pip`.
- **An administrator account.** The Portico panel, the Phones tab, the Elements tab and the editor are administrator-only. Phones and wall screens get their own non-administrator users.
- **Dependencies Home Assistant resolves itself.** The manifest declares `qrcode>=7.4` (Python package), the Home Assistant integration `file_upload` as a dependency, and `network` and `frontend` as soft dependencies. Nothing to do by hand.
- **Home Assistant's own `voip` integration** listens on UDP 5060. If you use it, it conflicts with the SIP port. **Unverified** on a live system; check `ss -lunp` for 5060.
- **Time zone and URLs.** Set the internal and external URLs under Settings > System > Network. Phones are enrolled with the **external URL if it is https, else the internal URL if it is https**. With only http URLs the enrolment refuses (`no_https_url`) unless you tick "Allow plain http on my own network".

## Installing the integration

Not available through HACS yet. The repository carries a `hacs.json` but is not published; what publishing needs is in [publishing](../development/publishing.md). Until then copy the folder:

```
cp -r custom_components/portico <config>/custom_components/
```

and restart Home Assistant. See [requirements and installation](install.md) for the symlink variant and the note about the old `portico_intercom` folder. When HACS publishing exists, the route will be "add the repository to HACS as a custom repository, install, restart"; that is the intended route and has not been run.

## The transport wheel

The real-time part (SIP server, RTP, WebRTC, the away audio leg) is a native program shipped as a Python wheel, `portico_transport`. The integration installs it itself the first time a door starts, and again only when the installed one is older than the version pinned in `custom_components/portico/door/wheel.py`. A normal restart makes no network call.

- **Architectures.** `x86_64` and `aarch64`. The pin map has no other entry, and a machine with no entry is handed the `x86_64` wheel. 32-bit ARM (`armv7`) is **not** supported by the current Rust wheels, even though older text said so.
- **Pinned version.** 0.7.1 (the Rust transport). The integration refuses to attach a door and raises the repair issue `wheel_too_old` if the installed wheel is older than 0.5.0.
- **How it is fetched.** Home Assistant's own requirement installer fetches the wheel from the URL in `WHEEL_PIN_URLS`, checking the `sha256` in the URL fragment. A failed install raises the repair issue `wheel_install_failed`; the door waits and the rest of Portico (phones, push, panel) keeps running.
- **Publishing blocker, stated plainly.** In the current source the pinned URLs point at an **internal** release host (a private Gitea on the maintainer's network) (`http://<internal-host>:3000/.../releases/download/transport-rs-v0.7.1/...`). A Home Assistant outside that network **cannot download the wheel**, so a stranger's installation does not work until the pin moves. The intended public route is a **release asset on the public repository** (or a PyPI upload of the same wheels); the pin, `MIN_WHEEL_VERSION` in `door/hub.py` and the wheel version move together. Neither is published. Until it is, a self-hoster can do the same by hand: download or build the wheel (`packaging/rust/`, Docker needed) and install it into Home Assistant's Python environment, for example from the Terminal add-on with `uv pip install <file>.whl`; the integration then finds it installed. **Unverified** end to end outside the maintainer's network. Tracked in [publishing](../development/publishing.md).
- **The wheel is a tagged `linux_x86_64` / `linux_aarch64` wheel** (not a `manylinux` or `musllinux` tag). It installs on Home Assistant OS. Other libc setups are **unverified**.
- **Rollback.** The pin never downgrades an installed wheel. Pinning an older version over a newer installed one is a no-op; uninstall the wheel by hand to go back.

## Network and ports

Home Assistant host is `192.168.2.5` here; the door station is `192.168.2.39`.

| What | Direction | Protocol and port | When |
| :--- | :--- | :--- | :--- |
| SIP registration and calls | station to the SIP server | **UDP 5060** | `gate_sip_server` |
| Call audio (RTP) | both ways, station and the SIP server | **one UDP port** you choose (form default 45000; container default 40000) | `gate_sip_server` |
| The station's own media port | server to station | a UDP port the station states in the call; seen at 20002, possibly a range `20000-20099` | **unverified** as a fixed range |
| Home Assistant web and websocket | phone, tablet, browser to Home Assistant | **TCP 443** (or 8123 behind your proxy), path `/api/websocket` | always |
| WebRTC media of a call answered on a phone or browser | phone to the machine running the transport | **UDP, ephemeral ports.** The transport binds one UDP socket per local interface on port 0 (a random port); there is no fixed range to open | at home; away through the tailnet address |
| Tailscale | Home Assistant and phones to the Tailscale control and relay servers; direct WireGuard UDP between nodes | UDP 41641 is Tailscale's usual port; see Tailscale's own documentation | for remote calling |
| Container SIP server's control link | Home Assistant to the container | one TCP port you choose (`-listen`) | container form only; not encrypted, private network only |
| RTSP to a camera or go2rtc | Home Assistant or phone to the camera server | TCP 8554 for go2rtc by default | live video away from home |

Notes that cost the maintainer time:

- **Across VLANs.** A default-drop rule between a network where the door station lives and the one where the SIP server lives silently blocks registration. The station retransmits, Home Assistant's log shows nothing. Open station to server for UDP 5060 and the RTP port, and let replies back through (established/related). Do not rely on a firewall read tool that shows zero rules: on the maintainer's site the live rules were not readable and the documented rules did not match experience. The first `REGISTER ... digest=OK` in the log is the proof.
- **One address.** Bind the SIP server to one address (`sip_host`), not `0.0.0.0`. The server then is not exposed on the tailnet or other interfaces. The form refuses standard ports until you tick `allow_non_loopback` and give `allow_from`.
- **Host networking for a container.** Behind NAT the addresses in the call's SDP and Contact header are wrong and the station sends audio to the wrong place. Use host networking. A bridge network with published ports is possible but must be rehearsed first.
- **Allowed sources.** The server accepts an INVITE only from the address of the current registration and answers `403` otherwise; BYE and CANCEL must come from the live call's address.
- **A second registrar.** One station has one registrar. Do not run two SIP servers for it.
- **No Docker network in your real LAN ranges** if you run a container: pick a private range that nothing else uses.

## HTTPS and a reverse proxy

- The Portico app accepts `https://` always, and plain `http://` only when the host is private (`10/8`, `172.16/12`, `192.168/16`, `169.254/16`, `100.64/10` which includes a tailnet, loopback, `.local`, `.lan`, `.home.arpa`, `.internal`, `.localdomain` and single-label names). A public name over http is refused. Enrolment prefers an https URL.
- Any reverse proxy in front of Home Assistant must pass **websocket upgrades** on `/api/websocket` and keep them open for a long time (the app holds one websocket per phone). Set Home Assistant's `http: use_x_forwarded_for` and `trusted_proxies` as Home Assistant documents.
- Tailscale's approve mode needs an **https** Home Assistant URL: its redirect carries an approval code, so plain http is refused.
- A certificate the phone does not trust (self-signed) fails the connection. Use a certificate from a public authority (the Let's Encrypt add-on or Tailscale's own certificates are two ways) or install your own authority on every phone. **Unverified** for self-signed.
- The phone dials the address the QR gives (`ha_url`). Away from home that name may not resolve, so the QR can also carry `ha_dial`, an IP and port inside the tailnet (for example `192.168.2.5:443`). See [enrolling a phone](../guides/enrolling-phones.md#what-else-the-qr-carries-ha_url-ha_dial-fcm).

## Tailscale for remote calling, step by step

Optional, but a phone away from home cannot reach Home Assistant, or carry a call, without some path in. Tailscale is the path Portico supports. Portico's app embeds its own Tailscale client (no VPN permission, no Tailscale app needed on the phone), and Home Assistant is itself a node on the tailnet. This page gives the order of the steps; [Tailscale](../guides/tailscale.md) has the modes and every error.

1. **Create a tailnet** at tailscale.com (free personal plans exist; plan limits are Tailscale's). Sign in as the person who will administer it.
2. **Put Home Assistant on the tailnet.** On Home Assistant OS install the Tailscale add-on, start it, and authenticate. Note Home Assistant's tailnet address (`100.x.y.z`) or MagicDNS name. In the add-on settings, accept the defaults unless you know you need a subnet route. Check in the Tailscale admin console under Machines that it appears and has no expiring key you will forget (disable key expiry for this machine).
3. **The machine that runs the transport must be a tailnet node.** The WebRTC leg offers the local addresses of the machine that runs the transport process, and it keeps tailnet addresses (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) whatever the interface is called, while excluding Docker bridges and loopback. A phone on mobile data reaches Home Assistant only at its tailnet address, so **a candidate on the tailnet interface is the one that matters**. This is why a SIP server **container on another host that is not on the tailnet fails mobile calls** (below).
4. **Create a tag** for phones, for example `tag:portico-phone`, in the tailnet policy file under `tagOwners`, owned by you. Do not reuse another application's tag.
5. **Write the access rule.** The tag must be allowed to reach Home Assistant: a rule from `tag:portico-phone` to your Home Assistant's address on port 443 (and 8123 if you use it). If you use live video away from home, also to the camera server's RTSP port. Portico cannot write the policy for you. Phones need no other access.
6. **Choose how phones get keys**, in the push entry's Configure > Tailscale mode: `auto` (Portico mints a one-hour, single-use, pre-authorised key for each phone from an OAuth client you give it) or `approve` (you approve each phone on Tailscale's consent page). Both are described, with the exact console steps, in [Tailscale](../guides/tailscale.md). For `auto`: Settings > Trust credentials > Credential > OAuth, scope `auth_keys` with Write, select your tag, copy the `tskey-client-...` secret once.
7. **Enrol the phone** from the Portico panel ([enrolling a phone](../guides/enrolling-phones.md)). The QR carries a one-time code and, in `auto` mode, a Tailscale key. The phone joins the tailnet on first use.
8. **Test away from home.** Turn the phone's Wi-Fi off, ring the door, answer. Check the transport log for a tailnet candidate and for STUN checks arriving within a second or two.
9. **When you remove a phone,** also remove its machine in the Tailscale admin console. Portico does not.

### The mobile-call failure to know about

On the maintainer's installation the SIP server was first tried in a container on a second machine that was **not** on the tailnet. The call rang on phones and never connected on mobile data: the SFU's ICE received **zero STUN checks** and went `Disconnected` after about 19 seconds, because the container offered only its LAN-interface address and a phone on mobile data cannot reach that. In Home Assistant, which is on the tailnet, the same SFU offers the `tailscale0` candidate and the earlier mobile call had STUN checks arriving from a tailnet address. Lessons:

- A container-only or LAN-only candidate fails mobile calls. The machine that terminates the WebRTC leg must be reachable at a tailnet address, or must advertise one.
- Docker bridge interfaces are excluded from the candidate list by default (`docker`, `br-`, `veth`, `virbr`, `cni`, `flannel`, `cali`, `lxcbr`, `lxdbr`, `podman`). `PORTICO_SFU_EXCLUDE_IFACES` (comma-separated name prefixes; an empty value excludes none) changes the list.
- Away-from-home **audio** has a second route that does not need the WebRTC UDP path: the phone can carry the call audio as 40 ms frames over the signalling websocket ([away call audio](../guides/away-call-audio.md), always on). The SFU's ICE failing is therefore a video and browser-call problem as much as a phone audio problem.
- The call media to the phone's tailnet node goes through Tailscale's relay if a direct path cannot be built. Quality depends on that path. **Unverified** across carriers.

## Frigate and go2rtc for live video

Optional. Tiles show a still picture every few seconds with nothing extra. Live video needs:

- **At home or in a browser:** Home Assistant's own camera WebRTC support, which uses go2rtc (Home Assistant's built-in go2rtc, or Frigate's). A camera with a go2rtc stream and a Home Assistant `camera` entity is enough. The panel's live window also uses Frigate's own go2rtc for a camera whose stream is a Frigate restream.
- **Away from home:** the phone pulls the camera's go2rtc stream over RTSP through its tailnet. This needs `live_rtsp_base` on the Settings tab (`rtsp://host[:port]`, an IPv4 address or host name, no credentials), a Frigate camera entity (the Frigate integration sets `camera_name` and `client_id` attributes), a `<camera>_sub` stream in go2rtc for each camera you list in `live_sub_streams`, and a tailnet access rule from the phone tag to that host and port (8554 by default). Anyone with a Portico phone on the tailnet who may reach that port can pull **any** stream on it, not only those you show them: say so before you open it.
- Details: [live video](../guides/live-video.md), [Settings tab](../guides/settings-tab.md).

## Push notifications

Optional. Without a push entry, phones at home or on the tailnet ring over the websocket. With it, a Firebase Cloud Messaging message wakes a phone that is away, asleep or whose websocket has dropped. You bring your own Firebase project and service-account key; see [push notifications](../guides/push-notifications.md) and [keys and secrets](../reference/keys-and-secrets.md). The Android package name is `com.meda.portico`. The app takes the four Firebase values it needs at runtime from the QR or a pasted `google-services.json`, so the same APK works for every household's project (**from the app's README; unverified on a second project**).

## Android phones

- Android 8.0 (API 26) or newer (`minSdk 26`). Tested on a few phones only.
- The Portico app, installed from an APK. The APK is signed with the project's release key; an APK signed with another key does not install over it. The APK is a release asset (a pre-release, "alpha", arm64 only) on the Releases page of https://github.com/gsasha/portico-app.
- Permissions the app asks for: notifications, the full-screen intent that shows the ring over the lock screen (on a sideloaded install this is a separate permission switch in Android's settings), microphone (to answer), camera (only to scan the enrolment QR), and unrestricted battery use so Android does not stop the connection. The app's Settings screen lists each problem with a fix button.
- Phones need to reach Home Assistant at the moment they enrol (home Wi-Fi is easiest).

## Wall tablets

Any screen with a browser that can stay logged in to Home Assistant: a tablet in a kiosk browser such as Fully Kiosk, or a browser kept open on a dashboard. See [the wall tablet](../guides/wall-tablet.md).
