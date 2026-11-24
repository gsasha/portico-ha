# Configuring a Dahua villa door station (VTO)

Step-by-step settings for a Dahua villa door station so that it registers to Portico's SIP server, rings Home Assistant and can be answered. Also how to check that it worked, what the failure signs look like, and how to put the station back.

Related: [supported hardware](../reference/supported-hardware.md), [prerequisites](../getting-started/prerequisites.md), [keys and secrets](../reference/keys-and-secrets.md), [first run](../getting-started/first-run.md), [AI-guided setup](../getting-started/ai-setup-prompt.md).

## What this page is based on

Everything below comes from the maintainer's own records of one installation: a Dahua `DHI-VTO3222E-P` villa station, firmware `4.810.0000000.1.R, build 2025-09-19`, 2-wire, which was moved from a third-party SIP server (`gate-sip`, a separate service, not part of this repository) to Portico's own SIP server in a supervised trial window. The author of this page did not touch a station. Statements are marked:

- **Observed**: written in the maintainer's records as seen on that one station.
- **From code**: read in this repository (`custom_components/portico/door/drivers/gate_sip_server.py`, `transport-rs/src/driver/gatesipserver/`).
- **Unverified**: not seen on a station by anyone whose notes I could read, or known to differ between models.

Other Dahua models and firmware may use other menu names. Addresses on this page are examples: the station is `192.168.2.39`, the machine that runs the SIP server is `192.168.2.5`.

## How the pieces connect

The station is a SIP **client**. It registers to a SIP server (a registrar) and sends a call (an INVITE) to that server when the visitor presses the button. Portico's `gate_sip_server` driver is that server: it answers the registration, rings the phones, and when someone answers it exchanges audio with the station over RTP.

```
visitor presses button
  station --REGISTER (about every 5 min)--> Portico SIP server  (UDP 5060)
  station --INVITE--------------------------> Portico SIP server
  Portico rings phones and wall screens
  someone answers: Portico --200 OK--> station, station --ACK--> Portico
  audio: station <--RTP over UDP--> Portico   (one port on the Portico side)
```

The station only takes **one** external SIP server. Whatever registered to that address before (another SIP server, a PBX) stops receiving the station once you change it. **Unverified**: the old notes infer the single slot from `Registar[0]` being an indexed list but never proved it.

### Which Portico driver

| You want | Driver | Status |
| :--- | :--- | :--- |
| The station registers to Portico, Portico rings and answers | `gate_sip_server` (menu entries "SIP-server door (advanced, not live)" and "SIP-server door, remote container (advanced, not live)") | Code complete. The container form (below) ran a supervised trial against a real station on 2026-11-18: LAN call worked, CANCEL and an immediate second ring were fine. The form that runs the server inside Home Assistant has not yet been run against a real station. |
| Portico registers **to** the station as if it were a phone | `dahua_sip` | In the code. The first real start (2026-10-03) timed out: with `Local SIP Server` off, nothing on the station answers a REGISTER. It is not activated against any station. **Unverified** for a station with `Local SIP Server` on, and that setting breaks calls (see "What not to do"). Do not use it for a new installation. |

The rest of this page is about `gate_sip_server`.

## Before you start

1. Read the station's current SIP settings and write them down (a screenshot of `Network Settings > SIP Server`, and an export of the configuration from `Maintenance Center > Maintenance Management > Config`). The exact labels differ between firmware versions, and you will want the values for a rollback. Keep the export off shared storage: it holds credentials.
2. Pick the **SIP password** now: 8 to 15 characters. It is stored on the station (`SIP.AuthPassword`) and in Portico, and the two must be identical. Below 8 characters the station sends **no REGISTER at all** while its own status can still read "success" (**Observed**; Portico's form refuses a password outside 8 to 15 characters).
3. Decide where the SIP server runs (below) and give that machine a fixed address. The station will be told this address; a changing address breaks it.
4. Make sure the network lets the station reach the server: UDP 5060 and the RTP port from the station to the server's address. See [prerequisites, network](../getting-started/prerequisites.md#network-and-ports). Across VLANs a firewall rule is often missing. A blocked path is silent on the station side (it retransmits, and nothing appears in Home Assistant's log). **Unverified** for your network.
5. Pick a time when nobody is expected at the door. Presses still ring if the SIP part breaks, but nothing can be answered until it works.

## Where the SIP server runs

Two forms of the same server (`portico-transport`, `gate_sip_server` driver):

| | In Home Assistant | In a container |
| :--- | :--- | :--- |
| Where | the transport process Home Assistant starts | a container on a machine you choose, [`packaging/standalone/`](../../packaging/standalone/README.md) |
| Address the station is told | the Home Assistant host | the container host |
| HA entry | menu "SIP-server door (advanced, not live)" | menu "SIP-server door, remote container (advanced, not live)" |
| SIP password lives in | the HA config entry | the container's secret; the HA entry holds none |
| Effect of a Home Assistant restart, wheel update or entry reload | **restarts the server and drops the registration.** An INVITE is refused (403) until the station's next REGISTER, which can take about 5 minutes. A press in that gap rings the house but cannot be answered (**from code and the maintainer's analysis**, measured gap: **unverified**) | none |
| Extra requirement | none | Docker, host networking recommended |

If your Home Assistant restarts often, or visitors come at any time, the container form is the safer permanent choice.

## Step 1: add the Portico SIP-server door

### In Home Assistant

Settings > Devices & services > Add integration > **Portico** > **SIP-server door (advanced, not live)**.

| Field | Value | Notes |
| :--- | :--- | :--- |
| `sip_host` | the Home Assistant host's LAN address, for example `192.168.2.5` | An IP literal, not a name. One address on purpose, not `0.0.0.0`, so the port is not exposed on every interface. Default `127.0.0.1` is inert. |
| `sip_port` | `5060` | The form refuses 5060 and anything under 1024 until both `allow_non_loopback` and `allow_from` are set. |
| `rtp_port` | one UDP port, for example `45000` | One port, not a range. The station learns it from the call's SDP, so any free port works. Must differ from `sip_port`. |
| `password` | the SIP password from "Before you start" | 8 to 15 characters. |
| `user` | `8001` (default) | The station's `SIP.UserID`. |
| `realm` | `VDP` (default) | The station's register realm. |
| `allow_non_loopback` | on | Required for a LAN address. |
| `allow_from` | the station's address, for example `192.168.2.39` | Required with the flag above. An INVITE from any other address is refused. A `/0` is refused. |
| `dial_enabled` | off | "Talk to the gate": lets Portico place a call to the station. Leave off until the ring works. |
| `ring_answered_entity` | optional `input_boolean` | Turned on once when Portico answers, so other screens close the ring as "answered elsewhere". |
| `confirm` | tick | The form refuses without it. |

Creating the entry only makes the server bind and wait. It does not contact the station.

### In a container

Follow [`packaging/standalone/README.md`](../../packaging/standalone/README.md): build the image with `docker build -f packaging/standalone/Dockerfile.rust` (see the README for the build context), start from [`compose.snippet.yml`](../../packaging/standalone/compose.snippet.yml) with host networking, set `PORTICO_SIP_LISTEN`, `PORTICO_SIP_ALLOW_NON_LOOPBACK=true`, `PORTICO_SIP_ALLOW_FROM=<station>`, `PORTICO_SIP_RTP_PORT`, the SIP password and an IPC token (24 or more characters). Then in Home Assistant: **SIP-server door, remote container (advanced, not live)**, with the container's IPC host (loopback or a private address) and port, the token, and the SIP address the container listens on. The IPC is not encrypted: keep it on loopback, a private Docker network or a tunnel. A `docker run` that is read-only, non-root and with all capabilities dropped is what the snippet describes. The image is not published in a registry (**Unverified** whether a public image will be).

A name, not an IP, is refused in both forms.

## Step 2: change the station

All values go on the station's SIP settings (web UI `Network Settings > SIP Server`, or the same leaves through the station's configuration). Four of the five are not guessable. Read them back after saving.

| Config leaf | Value | Notes |
| :--- | :--- | :--- |
| `SIP.SIPServer` | the address of the SIP server, `192.168.2.5` | |
| `SIP.OutboundProxy` | the same address | Set both. |
| `Registar[0].ServerType` | `H500` | A Dahua product code, stored as free text. The station accepts any text here (`Cloud`, `PBX`, anything) and gives no sign that a value is wrong. **Observed** to be needed for registering to an external server. The web UI label for this field was not recorded: **unverified**. The maintainer found it by dumping the whole configuration, changing the setting in the web UI, dumping again and comparing (7 of 16,083 leaves changed). |
| `SIP.UserType` | `0` | The default is `2`. |
| `SIP.IsMainVTO` | `1` | Leave it at `1`. `0` is wrong. |
| `SIP.AuthPassword` | the SIP password, 8 to 15 characters | Must equal the one in Portico. |
| `SIP.UserID` / `SIP.RegisterRealm` | `8001` / `VDP` | Defaults; they must match `user` and `realm` in Portico. |

Port fields (`SIP.SIPServerPort`, `SIP.OutboundProxyPort`) and a redundancy field (`SIP.SIPServerRedundancy`) exist on at least some firmware. If the redundancy field holds the old server's address, set it to the new one as well. **Unverified** which firmware has which; read them first.

**Does the station need a reboot after an address change?** **Unverified.** A change of SIP *mode* needed a reboot in both directions (**Observed**); until then the station sends nothing. An address-only change may re-register by itself. Wait up to 8 minutes for the first REGISTER; if nothing arrives, reboot the station. After a reboot, reload any other Home Assistant integration that watches the station's own events (for example Home Assistant's Dahua integration): the event stream dies on a reboot.

Other station settings that point at the old server, such as its NTP source, are separate. Leave them unless you mean to move them.

### Optional: read the settings over the station's API

A read-only way to see the values, if the web UI hides them. This is the station's RPC2 interface: `POST /RPC2_Login` with a challenge and an MD5 login, then `configManager.getConfig` with `{"name": "SIP"}` and `{"name": "Registar"}`, then `global.logout`. Each login uses one of the station's few sessions and a few failed logins lock the account, so: use the right credentials, stop after one or two failures, always log out. Do not write through it unless you know exactly what the write changes (a read before every write, so a concurrent change is not overwritten).

## Step 3: codecs and RTP

Nothing to set on the station. The server reads the station's offer and answers with the best codec it offers and the server can carry:

- **Wideband**: `PCM/16000` (16 kHz, 16-bit **little-endian**, which is not RFC 3551 L16). **Observed**: the station offers it and the audio is as good as the station's own path.
- **Narrowband** fallback: `PCMA/8000` (G.711 A-law).
- µ-law (`PCMU`) is not offered by the server. **From code.**

The station sends a packet every 40 ms (`ptime` 40), about 25 packets a second, with payloads of 1280 bytes on a wideband call. The station's own media port was seen at `20002` on one call; **unverified** as a fixed range (the maintainer's firewall proposal allows `20000` to `20099`).

## Step 4: verify it worked, from the log

`registered` flags and "success" statuses are not proof. On the maintainer's installation a health page reported `registered: true` thirteen seconds after a change, describing the first authentication failure in 24 hours, and the doorbell was dead for 85 minutes before a person noticed. **Verify from the server's log**, in this order:

1. `REGISTER from <station>:5060 digest=OK`. The station re-registers about every 4 minutes 58 seconds (`Expires: 300`), so a repeat after that interval is also a check. A first `unknown or expired nonce, re-challenging` followed by `digest=OK` is fine. `digest=MISMATCH` means the password differs from the station's.
2. After a real press: `INVITE from <station>:5060 to "<sip:9901@192.168.2.5:5060>"`. The call is addressed to the **server**. If it is addressed to the station itself, `Local SIP Server` is on (see below).
3. After someone answers: `ANSWERED - 200 OK sent`, then `ACK - dialog confirmed`.
4. The inbound RTP counter (`rtpIn`) climbs, about 25 per second. Registration is not media: a call that only increments `calls` proves signalling and nothing about audio. **`rtpIn` is the honest number.**

Where the log is: for the in-Home Assistant form, the Home Assistant log lines from the transport; for the container, `docker logs <container>`. A rejected RTP packet logs a line naming the reason (the server accepts RTP only from the address the station declared). A reject line with silent audio means that rule bit: record the line and roll back.

In Home Assistant: the door's call-state sensor (`sensor.<name>_call_state`) goes `ringing` on a press, and the `portico_intercom_call` event fires on the bus, with or without a phone.

### What a visitor and a household member experience

The station plays its own ringback tone because the server answers `180 Ringing`. If nobody answers in 45 seconds the server sends `480` and the station announces "not answered". A second press during a ring is refused busy (`486`). A visitor who gives up (CANCEL) clears the call, and the next press works.

## If it does not work

| You see | Likely cause | What to do |
| :--- | :--- | :--- |
| No REGISTER within 8 minutes of the change (6 after a reboot) | the path station to server is blocked, wrong address, or the station needs a reboot | Check your firewall for UDP 5060 and the RTP port from the station to the server. Reboot the station. If still nothing, roll back. |
| No REGISTER at all, and the station says "success" | password under 8 characters | Set 8 to 15 characters on both sides. |
| `digest=MISMATCH` | password differs between station and Portico, or a typo | Retype both. Roll back at once if it persists. |
| REGISTER `digest=OK` but a press gets `403` | the INVITE's source address is not the registered station's address, or the registration has expired (server restart: the binding is lost and lives 330 seconds) | Wait for the next REGISTER, up to 5 minutes. Check `allow_from` equals the station's address. |
| Rings in Home Assistant, "call was not answered" on the station, no `ACK`, `rtpIn` frozen | the station addressed the INVITE to itself: `Local SIP Server` is on | Turn it off (below). |
| Call connects, audio one way (phone hears nothing, or visitor hears nothing) | RTP port blocked in one direction, or a NAT between them | Allow the RTP port both ways. Use host networking for a container. |
| Audio sounds slowed down | a 16 kHz stream played as 8 kHz | Read the sample rate off the call, never assume it. (Portico does.) |
| Nothing rings but the station registered | the door's phone or screen is not enrolled or the call-state sensor is `unavailable` | See [first run](../getting-started/first-run.md). |

## What not to do

- **Never leave `Local SIP Server` on.** It makes the station its own SIP server. The doorbell still lights up a phone through another path, so the system looks alive while unable to complete a call. Two independent failures follow: the station re-authenticates against itself, and it re-addresses its INVITE to itself (the `ACK` never comes and no RTP flows).
- **Do not use `Cascade SIP Server`** to be both. It registers, the web UI un-greys, health pages say healthy, and calls still do not work. It is not documented in the villa door station manual (the manual's section on cascading is a different feature).
- **Do not trust a "registered" flag** on any health page, including the station's own `getSIPRegState`. Use the log.

### Keypad codes and the "one-time flip" trap

Some firmware greys out `Person Management > Add` and `Access Control > Public Password` while the station is a pure SIP client, so no keypad code or card can be created. The workaround the maintainer used (**Observed on one station; the mechanism is not confirmed**; do not assume a factory-reset station behaves this way):

1. Record the settings and export the configuration (see "Before you start").
2. Enable `Local SIP Server`. The station reboots.
3. Create the credential: `Access Control > Public Password` (a shared code, simplest) or `Person Management > Add` (per person; set Lock Permission to the local lock).
4. **Test the code at the keypad before reverting.**
5. Turn `Local SIP Server` back **off** and restore the values in Step 2.
6. Verify again from the log (Step 4).

Stored credentials survive the revert, and the credential screens stayed editable afterwards on that station. Keypad entry formats vary by firmware; try `#<code>#` (shared code), `<code>#`, then `#<room-number><personal-password>#`. The `Unlock Code` field on the Access Control page is **not** the keypad code: it is a DTMF string a SIP phone sends mid-call to open the door, and Portico does not send it. If the station says "unlocking" but the door does not open, the station actuated its relay; look at the lock wiring, not the credential.

## Opening the door

A SIP call does not open the strike. Portico opens a door through a Home Assistant action you choose (a `button`, `lock`, `cover` or `script` entity) as a button on a phone's or screen's page, see [content, pages and actions](content-pages-and-actions.md) and [Elements](elements.md). Create that entity in Home Assistant first (for example with Home Assistant's Dahua integration), then add the button. Each press goes through a second tap for locks and gates.

## Putting the station back (rollback)

1. Set `SIP.SIPServer` and `SIP.OutboundProxy` back to the values you wrote down. Reboot the station only if the first change needed one.
2. Reload any integration that watches the station's events.
3. Wait for a REGISTER at the old server (about 5 minutes), then do one real press and check that the old path rings and answers.
4. Leave the Portico door entry loaded and idle, or disable it. Do not run two registrars for the same station: the station has one slot.

Budget 10 to 15 minutes. The pre-SIP originals the maintainer's station had before any of this (`ServerType VTO`, `UserType 2`, `SIPServer` = the station's own address, `IsMainVTO 1`) are the second fallback. **Observed once on one station.**

## Station facts worth keeping

- Access to the station's own interface needs an administrator account. Portico does not store it for `gate_sip_server`: the SIP password is the only credential this driver uses. The `dahua_sip` driver also takes an RTSP user and password for video; `gate_sip_server` does not, and the station's video comes from a Home Assistant camera entity (see [live video](live-video.md) and the camera step of the door form).
- A press also fires an event in Home Assistant's Dahua integration (a "button pressed" binary sensor) that does not depend on the SIP server. On the maintainer's installation an automation on it rings phones even when the SIP server is down. Portico's own ring comes from the INVITE.
- A station allows only a few simultaneous sessions (a further one gets `486 Busy Here`), which includes the web UI, RPC2 logins and a monitor client.
