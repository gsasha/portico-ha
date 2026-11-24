# The door side (`custom_components/portico/door/`)

This package is the part of Portico that talks to doors. It owns the real-time transport (one bundled native process: Rust since wheel 0.7, Go before),
the per-door call state and the door entities, and it tells the provider registry which doors exist and what they
are doing. It was the separate integration `portico_intercom` until the unification (DEV-686); it is now a package of
the one integration `portico`, and a door is a `portico` config entry with `entry_type: "door"`.

Doors are configured, the call-state sensor updates and the `portico_intercom_call` event fires on the HA bus, so an
ordinary automation can react to a ring, with no phone and no push ring notification configured.

It needs the `portico_transport` wheel (see below). See the repository root `README.md` and [`docs/`](../../../docs/README.md) for installation and the
whole picture.

**Names that did not change with the fold** (they are persisted or consumed elsewhere): the door provider id
`portico_intercom` (`const.DRIVER_ID`; the door's endpoint id is `portico_intercom:<device_id>`), the bus event
`portico_intercom_call`, the entity ids `sensor.call_state` and `sensor.listen_leg` and their unique ids, the hub socket
`.storage/portico_intercom/hub.sock` and the peer-token Store key `portico.gate_sip_peer.<entry id>`. What changed is the
HA domain (`const.DOMAIN`, `portico`: the entity registry's platform, repair issues, the flow handler).

## Drivers and adding a door

A *door* is one config entry. Settings > Devices & services > Add integration > **Portico > Add a door**,
pick a driver, fill its fields, then optionally point the relay at an HA action and at an
existing camera entity. Away call audio is always on and has no option (a stored `away_media` option is ignored).
A `gate_sip_server` door (its own menu item, not in the picker) has its switches in Reconfigure.

| Driver key | Door it drives | Fields |
| :--- | :--- | :--- |
| `dahua_sip` | A Dahua villa station (VTO) reached over SIP, with its RTSP video. | `host`, `sip_user` (the `user#index` form), `sip_password`, `rtsp_username`, `rtsp_password` |
| `ha_event` | A **ring-only** door: the ring comes from a Home Assistant event entity (for example a UniFi Protect doorbell's `event.*` entity, type `ring`). No transport driver, no wheel change, no console connection, no transport process of its own. States no audio and no relay: a phone gets the ring screen with the door's camera and cannot answer, talk or open. See "The ring-only `ha_event` door". | `event_entity` (an `event.*` id), optional `name` (default `front`; the entry title and the door's name), optional `event_type` (default `ring`), optional `ring_hold` (seconds, 5 to 120, default 30); the camera step is required (the doorbell's own HA camera) and the relay step is not offered |
| `unifi_protect` | A UniFi Protect doorbell camera: video and talkback through the console's API; ring detection is optional. | `nvr_host` (the Protect console), `nvr_api_key`, `camera_id`, optional `verify_ssl`, optional `ring_events` |

A door is identified by a stable `device_id` that is never a Home Assistant entry id (for
`gate_sip_server` it is `gate-sip-server:<host>:<port>`), so re-adding an entry keeps the door's
identity. A second entry for the same device is refused.

Each driver has two halves: its config-flow schema in `drivers/` (Python, no runtime logic) and its
runtime in the Rust transport (`transport-rs/src/driver/`). The flow's door steps are `flow.py`, mixed into the
integration's one `PorticoConfigFlow`.

## The ring-only `ha_event` door

`drivers/ha_event.py` (schema, capabilities) and `ha_event_ring.py` (the ring source). The door never attaches to the
hub (`DoorRuntime.ring_only`), so adding or removing it does not touch the transport process, the wheel, or any other
door. A press is a change of the event entity to a fresh timestamp with `event_type` = the configured type and an
`event_id` not seen before; it starts a call `ha-event-<event_id>` on the door's tracker, which ends by itself after
`ring_hold` seconds or when a phone dismisses it. Not a press, so no ring: the event the entity already held when the
door started, an `event_id` seen before, an event whose own timestamp is older than 30 s (a restore after a restart or
`unavailable`), another event type, Home Assistant not yet running, a second press within 3 s. The door is unavailable
while the event entity is unavailable or missing. The door's call-state sensor is `sensor.<name>_call_state`.

Ring-only is enforced three ways: the driver's Capabilities state `HasRelay` false, `DirectCapable` false and no audio, so
no lock entity exists; the runtime's client is a stand-in that refuses `answer` (verdict `refused`), `open_relay`, WebRTC
and the away-media leg; and the provider refuses `open_relay` for the door whatever an entry holds. The ring source calls
no service.

Adding the front door (a Home Assistant write, done by an administrator): Settings > Devices & services > Portico > **Add a
door** > driver `ha_event` > `event_entity` = `event.front_door_doorbell_ring`, name `front` > camera =
the doorbell's HA camera entity > submit. No restart is needed on a Portico build that has this driver loaded (a custom
component update itself needs one). Then, in the Portico editor, check which phones' ring screens use the new door (the pilot: one phone) and watch one real
press. The push ring notification goes to every phone switched on for the push ring notification, as for any door.

## The one hub process and the wheel

One `portico-transport` process serves the whole HA instance (`hub.py`). The first door that attaches
starts it, each door is `configure`d into it by its `device_id`, a crash is restarted once and every
attached door is re-configured, and it stops after the last door detaches. Python and the process talk
over a Unix socket in the HA config directory (`.storage/portico_intercom/hub.sock`) with
newline-delimited JSON-RPC (`transport.py`). The process serves three media roles: the drivers, the
browser WebRTC bridge, and the away-media frame leg.

The transport binary is not shipped as source in the component. It is built into a platform-tagged wheel,
`portico_transport` (x86_64 and aarch64; the pin map in `wheel.py` has no armv7 entry), published as a release asset. The pin is two constants in
`wheel.py` (`WHEEL_PIN_VERSION`, `WHEEL_PIN_URL`), NOT a manifest requirement: a manifest URL requirement is handed to
pip on every load, which would make the whole integration fail to load whenever the package host is down. Instead the hub
calls `wheel.async_ensure_wheel` when the first door attaches, BEFORE its own version check: when the installed wheel is
older than the pin (or missing) it installs the pin with Home Assistant's requirement installer, clears HA's sticky
install-failure history first and calls `importlib.invalidate_caches()` after; on a normal boot it only compares
versions and makes no network call. A failed install raises the repair issue `wheel_install_failed` and
`ConfigEntryNotReady` (the door waits; phones, push and the panel run). `hub.py` then checks the installed wheel against
`MIN_WHEEL_VERSION` (currently `0.5.0`; the pin is `0.7.4`, the Rust transport) and raises a repair issue (`wheel_too_old`) instead of
attaching a door if the wheel is still older than the Python expects. The current wheel is built from `transport-rs/` (Rust; build, tags `transport-rs-v*` and the rollback rule are in `packaging/rust/README.md`). The older Go wheels (0.6.x, tags `transport-v*`) were built from a Go `transport/` that is deleted from `main` (in git history, commit `bdd8e61`); there is no rollback to them (D-20261119-c1af). Because the installer installs only when the installed
wheel is OLDER than the pin, repinning to any lower wheel is a no-op.

## Entities

Per door entry:

| Entity | Notes |
| :--- | :--- |
| **Call state** sensor (enum: `idle`, `ringing`, `answered`, `active`, `ended`) | Push-driven. Becomes `unavailable` when the transport is down or, for a driver that reports its device's availability (`dahua_sip`, `unifi_protect` with ring events), when the device cannot be reached, rather than showing a false `idle`. Attribute `call_id`. |
| **Relay** lock | A momentary strike: always locked, `open` pulses it. If a door has an open-relay HA action configured, that action runs; otherwise the driver's own relay command is sent. |
| **Camera** | Backs the browser video path. Not created when the door is pointed at an existing camera entity. |
| **SIP station registered** binary sensor (diagnostic, `connectivity`) | `gate_sip_server` entries only. `on` while the door station is registered with Portico's SIP server (a press from an unregistered station gets 403; after an HA restart that can last about 5 minutes). From wheel 0.7.2 it follows the driver's `StationRegistered` capability, published on every good REGISTER, at the 330 s expiry and on a de-register, whether or not `dial_enabled` is on; the `last_register` attribute (UTC ISO time, from `LastRegisterAt`) shows how old the last REGISTER is. With an older wheel it falls back to `CanDial` (published only with `dial_enabled` on), and is `unavailable` while that switch is off. `unavailable` too while the transport is down. |

The doors, their availability and every call transition are also published to the provider registry (the
websocket commands, the push sender, content) through the provider protocol; `async_setup` registers the provider.
The protocol is [`contract/provider_v1.md`](../../../contract/provider_v1.md).

**House calls (M7 3b-S, inert).** The hub passes `-house-calls` to the transport binary only when the installed wheel is 0.6.0
or newer (read from the wheel's metadata) **and** a door entry's option `house_calls` is exactly true (default off; a door's **Reconfigure** form writes it since M7 3g and shows it only with such a wheel; it is read when the process
starts, so a change takes effect at the next hub start, in practice the next Home Assistant restart). A 0.5.x binary exits on
an argv flag it does not know, so it is never given one. `MIN_WHEEL_VERSION` and the manifest pin do not move for this. The provider
gains the optional house-call methods of `provider_v1.md` section 4b (`house_bridge_supported()` is the feature
detection); with a 0.5.x wheel, or the option off, every one answers `not_supported` and sends nothing. `house_leg_state`
notifications are routed to those listeners by call id, never to a door.

## The bus event `portico_intercom_call`

Every call transition is fired on the HA event bus as `portico_intercom_call`, whether or not any phone is enrolled. `event.data`: `device_id`, `state` (`idle`, `ringing`, `answered`, `active`,
`ended`), `call_id`, `prev_state`, `prev_call_id`, `at_wall` (epoch seconds). An automation can
trigger on it:

```yaml
triggers:
  - trigger: event
    event_type: portico_intercom_call
    event_data:
      state: ringing
```

## The gate_sip_server door's Reconfigure form

The `gate_sip_server` entry's **Reconfigure** form has: **Talk to the gate from Portico** (`dial_enabled`, off), the
**ring-answered helper** (an `input_boolean` turned on once when Portico's user has answered, so other screens close the ring as
answered elsewhere; the only HA write made at answer time), the **door-open action** (`open_relay_action`) and, in remote mode, the
container's address and token. An edit that touches only the helper, the action or the option below is saved without a reload (the
registration is kept); a change to `dial_enabled` or the remote fields reloads the entry.

* **Phone-to-phone house calls** (off, needs transport wheel 0.6.0): shown only when the installed wheel has the bridge (or when already on, so
  it can be switched off), stored as the door entry's option `house_calls` and only when on, and refused with `house_needs_wheel` when
  saved on with an older wheel. The Portico hub reads it only when its process starts, so a change takes effect at the next Home
  Assistant restart. It rings no one by itself; the per-phone **Receive** switch is separate.

The gate-sip observer door (the listen leg, claims, the claims switch) was retired (DEV-518 C5).

`unifi_protect` has a similar opt-in, `ring_events`, off by default: when set, the transport
subscribes to the console's public events websocket to detect a doorbell ring.

## Tests

From the repository root: `scripts/test_offline.sh custom_components/portico` (the venv is
described in [`docs/development/running-tests.md`](../../../docs/development/running-tests.md)). The door side's tests are in `custom_components/portico/tests/`:
`test_observer.py` (the shared door fixtures, the sensor, the lock), `test_hub.py`, `test_transport*.py`, `test_gate_sip_*.py`,
`test_wheel.py` (the lazy install), `test_door_fold.py` (the domain/provider-id split, the options flow and diagnostics by
entry kind). `tests/door_provider_conformance.py` is the conformance suite for the provider protocol.

The Rust transport has its own tests: `cargo test` in `transport-rs/` (see `transport-rs/README.md`).
