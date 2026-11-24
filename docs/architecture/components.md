# Components

The parts of Portico, one paragraph each, with where the code lives and where more is written.

Up: [architecture overview](overview.md).

## The Python integration (`custom_components/portico/`)

One Home Assistant integration, domain `portico`: the `portico/*` websocket commands the phone app, the card and the panel speak (`signaling.py`, authorisation in `auth.py`), enrolment (`enrollment_*.py`, `enroll_*.py`), the content store and its commands (`content*.py`, `pages_*.py`, `elements*.py`), the push ring notification (`push/`), house calls (`house*.py`), and the config and options flows. Its own README: [`custom_components/portico/README.md`](../../custom_components/portico/README.md). Commands and who may send them: [authorisation table](../reference/websocket-auth.md).

## The door side (`custom_components/portico/door/`)

The former integration `portico_intercom`, now a package of `portico` (DEV-686): the hub that starts and talks to the transport process, call state, door entities (`sensor.call_state`, `sensor.listen_leg`, camera, lock), the drivers in `drivers/` (`gate_sip_observer`, `gate_sip_server`, `dahua_sip`, `unifi_protect`, `ha_event`) and the lazy wheel install (`wheel.py`). Its README: [`door/README.md`](../../custom_components/portico/door/README.md).

## The transport (`transport-rs/` Rust, wheels in `packaging/`)

One `portico-transport` process per Home Assistant instance carries SIP, RTP, WebRTC and the away-media leg; the integration starts it and talks to it over a Unix socket (JSON lines). It is delivered as the pip wheel `portico_transport`, installed by the integration. [The process](transport-process.md); Rust port: [`transport-rs/README.md`](../../transport-rs/README.md); packaging: [`packaging/rust/README.md`](../../packaging/rust/README.md); versions: [versions and tags](../reference/versions-and-tags.md).

## The content store

Per-phone and per-screen documents (pages, blocks, cameras, buttons) in `.storage/portico.content`, and the shared elements in `.storage/portico.elements`. [Content](../guides/content-pages-and-actions.md), [Elements](../guides/elements.md).

## The signalling websocket

Everything a client does goes over Home Assistant's own websocket as `portico/*` commands, "contract 3". Events: [call-state and content events](../reference/call-state-and-content-events.md). Recorded fixtures: [contract fixtures](../reference/contract-fixtures.md).

## The card and the panel (`custom_components/portico/www/`)

The `portico-card` Lovelace card (dashboard and wall-tablet kiosk page) and the admin panel with the Pages, Phones, Elements and Settings tabs. [Card and panel](../guides/dashboard-card-and-panel.md), [Settings tab](../guides/settings-tab.md).

## Door drivers

Each driver has a config-flow half in `door/drivers/` (Python) and a runtime half in the transport. The table of drivers and their fields is in [`door/README.md`](../../custom_components/portico/door/README.md).
