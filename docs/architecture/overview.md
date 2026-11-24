# Architecture overview

What Portico is made of and how a ring travels from a door to a phone: one Home Assistant integration, one bundled transport process, and the websocket contract that phones, the dashboard card and the admin panel speak.

Next: [the transport process](transport-process.md), [components](components.md). The repository layout is in [development/repository-layout.md](../development/repository-layout.md).

## What Portico is

The Home Assistant side of **Portico**: a config-driven calling plugin for door stations, gate
controllers and doorbell cameras. A visitor rings, Home Assistant knows about it, the phones in the
house (and phones that are away) ring, someone answers from an app or a dashboard, and the door can
be opened. It ships as **one Home Assistant integration** (domain `portico`), plus a bundled
transport process (Rust since wheel 0.7; Go before) for the real-time parts (SIP, RTP, WebRTC).

Status: the integration is one (it was two, `portico` and `portico_intercom`, from the split of
2026-10-20 until the unification, DEV-686), the transport runs as one process per Home Assistant
instance, and the code is not published on HACS yet (see [publishing](../development/publishing.md)).

| Part | Folder | Domain | Version | Tag | What it is |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Integration | [`custom_components/portico`](../../custom_components/portico/README.md) | `portico` | 0.3.0 | `portico-v*` | phones, enrollment, content, the push ring notification, the card and panel, the `portico/*` WebSocket commands, and the doors: the transport process, call state, door entities, the `gate_sip_observer` / `dahua_sip` / `unifi_protect` drivers ([`door/`](../../custom_components/portico/door/README.md)) |
| Transport wheel | `transport-rs/`, `packaging/rust/` | (pip package `portico_transport`) | 0.7.1 (the pin in `door/wheel.py`) | `transport-rs-v*` | the Rust binary, built into a platform-tagged wheel (`x86_64`, `aarch64`) the integration installs itself (the pin is `door/wheel.py`, not the manifest). The Go transport it replaced (tags `transport-v*`) is gone from the tree, with no rollback to it (its source is in git history) |

There are three kinds of config entry in the one domain, told apart by `data["entry_type"]`: `push` (the push ring notification, the
install-wide settings host), `phone` (one per enrolled phone) and `door` (one per door, with its own `data["driver"]`).
Doors are added with "Add integration > Portico > Add a door". The door side works without a phone or the push ring notification
(doors, call-state sensor, the `portico_intercom_call` bus event). The provider protocol between the door side and the
registry that serves the websocket commands is [`contract/provider_v1.md`](../../contract/provider_v1.md); with one built-in
provider it is an internal seam.

## The big picture

```
  phone app / dashboard card / admin panel
          |  WebSocket: portico/* commands (contract/ws_v3.json)
          v
 +------------------------------+   FCM push (push ring notification)   +----------+
 | portico   (one integration)  |------------------------->|  phones  |
 |  signaling, auth, enrollment |                          +----------+
 |  content, endpoints, push    |
 |  provider registry           |
 +---------------^--------------+
                 | provider protocol v1 (plain dicts, contract/provider_v1.md), one built-in provider
                 |
 +---------------+--------------+     bus event portico_intercom_call
 | portico/door/                |---------------------------------> automations
 |  door entries, entities,     |
 |  call state, provider_impl   |
 |  hub.py: ONE process         |
 +---------------|--------------+
                 | Unix socket, JSON-RPC  (.storage/portico_intercom/hub.sock: a historical folder name)
 +---------------v--------------+
 | portico-transport  (Rust)    |  wheel portico_transport, built from transport-rs/
 |  drivers: gate_sip_server,   |
 |   dahua_sip, unifi_protect   |
 |  WebRTC bridge, away media   |
 +---------------|--------------+
                 | SIP / RTP / HTTP / RTSP
              the doors
```

## Vocabulary: the UI part and the driver part

"UI part" and "driver part" are the two halves of the one integration: the former is everything in `custom_components/portico` outside `door/`, the latter is `door/`.
