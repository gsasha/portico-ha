# Portico

`custom_components/portico` is the whole Home Assistant side of Portico, one integration. It serves the
`portico/*` WebSocket commands the Portico phone app and the dashboard card speak, enrolls phones,
decides what each phone shows, pushes the "someone is at the door" ring to phones that are away
from home, ships the Lovelace card and the admin panel, and talks to the doors.

Doors (a SIP intercom, a gate controller, a camera doorbell) are the integration's `door/` package
([`door/README.md`](door/README.md)): the transport process, the call state, the door entities and the three
drivers. A door is a config entry with `entry_type: "door"`; the websocket commands reach it through an internal
provider registry that holds one built-in provider. With no door configured the integration still loads: it
enrolls phones and serves content, and `portico/doors` lists no doors.

Version `0.3.0` (contract 3; tag `portico-v0.3.0`). Domain `portico`. Python requirement: `qrcode` (the
enrollment QR). The transport wheel is NOT a manifest requirement: `door/wheel.py` installs it when a door starts and
the installed one is older. HA dependencies: `file_upload`; soft: `network`, `frontend`.
See the repository root `README.md` and [`docs/`](../../docs/README.md) for installation, testing and the layout of the whole project.

## What it does

| Area | What you get |
| :--- | :--- |
| **Phones as entries** | Every enrolled phone is its own config entry (one per phone) with a device, a sensor for its push registration state and last connection, and a Configure menu for the doors it may use. Removing the entry revokes the phone: its tokens (and the HA user enrollment created for it), push registrations and content override are removed; the phone's Tailscale node is not, a notice names it. |
| **Enrollment** | The push entry's Configure menu creates a dedicated HA user for a phone, a one-time code (the QR carries no token; the phone redeems the code for its own sign-in) and (depending on the Tailscale mode) a network key, and shows one QR code the app scans. |
| **The Tailscale answer** | How a phone reaches Home Assistant from outside the home network, chosen per install: `approve` (the default: the phone's app joins the tailnet with its embedded client and an admin approves it on Tailscale's consent screen through an OAuth app), `auto` (a key is minted automatically from an OAuth client or API credential), or `manual` (the phone uses the official Tailscale app and nothing is minted). The mode can be changed later from the push entry's Configure menu without removing anything. |
| **The push ring notification** | When a visitor rings and a phone is not at home, the integration sends an FCM (Firebase Cloud Messaging) high-priority message so the phone rings like a call. You bring your own Firebase project and service-account key; the key is kept in a private store (file mode 0600). A staged real-device test (`portico.push_test`) is included. |
| **Away call audio** (always on; no option) | A phone that reaches Home Assistant only through its embedded tailnet client can carry the call audio as frames on the same WebSocket (`portico/media_*`). |
| **Content and screens** | Each phone has a *content profile*: an ordered list of cameras and actions (open a door, press a button, run a script or scene, place a call). The profile is the grant: a phone can run only what is in its own profile. The install-wide *home name* is kept in the same store. Pages, cameras and buttons are edited in the Portico panel (sidebar), not in Configure. |
| **The endpoint registry** | One read-only list of who a call can name: phones (by enrollment id) and doors (`<provider domain>:<device id>`). It is derived, holds no state, and survives a door being removed and re-added. |
| **Panel and card** | `portico-card` is a Lovelace card (a phone's screen on a dashboard, a door ring on a wall tablet). `portico-panel` is a sidebar page for every HA user: an administrator edits and sees what each phone shows (tabs **Elements** (first, opened by default), Pages, **Screens**: declare a kiosk screen, its HA user, room cameras and its kiosk address; **Ring & call**: the per-caller rules for what a ring shows, with a preview), anyone else is limited by the auth table (every write is `admin`). Opened at `/portico/kiosk/<screen address>` the panel is the kiosk page: it says `portico/kiosk/hello` and then draws only `<portico-card endpoint=<address> fullpage>`. Both are served from `/portico/` by this part; HA needs no extra resource entry. |
| **Diagnostics** | Redacted diagnostics downloads for every entry. |

## The `portico/*` WebSocket commands

The commands (contract 3, no aliases) are `portico/version`, `portico/doors`, `portico/calls/subscribe`,
`portico/content/subscribe`, `portico/answer`, `portico/dial`, `portico/hangup`, `portico/open_relay`,
`portico/keepalive`, `portico/webrtc_offer|answer|ice`, `portico/content/get|run|snapshot`, `portico/content/live|live_ice|live_close` (live video of a camera tile: WebRTC, video only, through Home Assistant's own camera support; [`docs/guides/live-video.md`](../../docs/guides/live-video.md) has the frames), the editor's
`portico/content/document|preview|save` and `portico/kiosk/hello`, the panel's `portico/camera/webrtc_offer` (administrators only; `{entity_id, sdp}` -> `{answer}` from Frigate's own go2rtc for a camera whose stream is a Frigate restream, else the error `not_frigate_stream` and the live window uses HA's `camera/webrtc/offer`; frigate_webrtc.py), the panel's `portico/cameras/info` (administrators only; the picture size and, when Home Assistant knows it, the frame rate of up to 20 camera entities, for the camera picker's spec chips; the phone app never sends it), `portico/push/register|unregister|status`, plus
`portico/media_open|frame|close` for away call audio, and the five house-call commands `portico/house/answer|decline|hangup|presence|media_open` (M7 3a, 3b-S; `not_available` while house calls are off, which is always today). Contract 2's `devices`, `subscribe` and `content/list` are gone. Every handler checks who is calling first; one table (`auth.py`) says which of `admin`,
`phone`, `dashboard` and `any` may send which command.

`portico/content/get|run|snapshot|live|subscribe` take one optional key, `endpoint`. An enrolled phone gets
its own endpoint (naming another is `unauthorized`); a *declared screen* (an HA user a dashboard is declared for in the
content store) gets the dashboard `endpoint` names, or its only one (a login that serves several must name one); an
administrator names any phone or dashboard by `endpoint`. `content/get` answers the endpoint's document: `endpoint`,
`kind`, `rev`, `home_name` when set, `pages`, `callers`, `roster`; `content/run`, `content/snapshot` and `content/live` are granted
over that resolved document (every page, and the `c-...` cameras of its callers). `portico/doors` carries each door's
`door_id`, `door_ref` (the derived push ref), `endpoint_id` (the key of `callers`), `name`, `driver`, `capabilities`,
`camera_entity_id`, `available` and `away_media`, and no secrets. The call commands name a door by `door_id`; an
unknown one is `unknown_door`. Call-state events come from `calls/subscribe` (one snapshot per door, then
`{door_id, call_id, state, prev_state}` events, no `idle` echo after `ended`) and `content_changed {endpoint, rev}` events
from `content/subscribe`; the two never share a stream. `portico/push/register` refuses an `app_version` below
`0.5.0` with `app_too_old`: app 0.4.3 speaks contract 2 and cannot read a `pv: "2"` push.

The contract is recorded as fixtures, one case per request with every frame sent back:

* `contract/ws_v3.json`: contract 3 (`portico/version` returns `{contract: 3, ui_version, providers}`): every
  command and caller, with each command's authorization class. The phone app replays it in its own tests.
* `contract/app_commands_v3.txt`: the commands the app sends; both sides test against it.
* `contract/ws_media_v2.json`: the away-media frames.
* `contract/push_v2.json`: the FCM data messages (`pv: "2"`, with `ct` and `from_ref` for the house calls; v2.1, M7 3c, appends the house-ring cases)
  as a phone receives them, and the reaction expected.
* `contract/icons_v1.json`: the icon names the icon picker offers.

The fixtures live in the repository's `contract/` directory; see [`docs/reference/contract-fixtures.md`](../../docs/reference/contract-fixtures.md) for what each
is for and how to regenerate it. The subscription events and the content model are described in
[`docs/reference/`](../../docs/reference/call-state-and-content-events.md) and [`docs/guides/`](../../docs/guides/content-pages-and-actions.md).

## House calls: the server model (M7 3a; always on since D-20261122-aafb)

`house.py` is the registry of calls between two endpoints of the house (a phone, a declared screen) and from a screen to
every phone that receives ("Everyone"); `house_ledger.py` is the `ClaimLedger` (one house call per endpoint, a door
outranks a house call, four house calls at once, two door calls per user for hold and switch); `house_commands.py` the
four commands. Placing a call goes through `portico/content/run` (a roster key or a `call` button), progress arrives as
`house_call` events on `portico/calls/subscribe`, and a presence change as `house_roster_changed {rev}`.

**It is always on.** The registry is built at domain setup. There is no global House calls switch (removed by D-20261122-aafb:
who is called is decided per action, by the Talk and group elements); the settings entry's old `house_calls` option is ignored.
The door flow, `sensor.call_state` and the push are what they were (`tests/test_house_golden.py`, run with the registry off, proves it against a flow recorded from `main`). A phone receives house calls only with
its entry option `house_receive` on (default off, no UI yet: HC-2). There is no media (a call that is answered ends
`error` at the 10 s audio deadline) and no client. **The push ring notification (M7 3c, off with the model):** a callee with `house_receive` on and
no open `calls/subscribe` is "away"; when the push entry's push ring notification is on, its credentials are not known bad and the phone has a
live registration, it is `push_only` (presence, and the call button's `push_only`), may be called, and is rung by a `ct: house`
push (`push/notifier.py` `house_ring`, `house_answered`, `house_left`, `house_ended`: a path of its own, never the door's) with an
opaque `from_ref` (`house_from_ref`) and no `door_ref`. A phone in Do Not Disturb with its socket open is refused as before; the
winner of a first-answer and a callee that declined hear nothing more; the 5 s / 20 per hour per-callee cap, the 60 a minute circuit
and the staged-test allow-list are the door ring's. The old intercom system (`house-talk`, the `input_text.intercom_*` helpers, the kitchen
panel's own house card, which belongs to the maintainer's dashboards) is neither read nor written; `tests/test_house_boundary.py` checks it on the source.

**Streams already open.** `house_attach.py` lists the open `portico/calls/subscribe` streams (the switch that used to re-attach them on a change is gone; a stream opened after setup attaches itself). The audio of a house call is a separate door setting, **House calls: audio
between phones** in the Settings tab (a door's Reconfigure form, `house_calls`).

## House calls: the media glue (M7 3b-S, inert)

`house_bridge.py` joins the call model above to the hub's two-leg bridge. Placing a call and answering it open nothing; once a
call is `answered`, each end sends **`portico/house/media_open {call_id, codec | sdp}`** to declare its audio leg (no `sdp`:
a frame channel over the socket, `codec` `pcmu` or `l16`, default `pcmu`; an `sdp`: a WebRTC offer). The hub needs both legs
at once, so the first end's command waits and the second opens one bridge (`from` = the caller, `to` = the callee; the
others that were ringing never get a leg). Result `{kind: "frames", codec, frame_bytes, frame_ms}` or `{kind: "webrtc"}`,
then events on that command id: `{t: "f", d}` downlink frames, `{t: "sdp", sdp}` the WebRTC answer, a last `{t: "end",
reason}`. Uplink is the shared `portico/media_frame {call_id, d}` and the goodbye `portico/media_close {call_id}` (an `hso-`
id is handed to the bridge by `away_media.py`; every other id takes the door path it always took). The call becomes `active`
on the callee's first frame (`house_leg_state first_frame` for `to`). It ends, and the bridge is closed once, on a hang-up
from either end, the 45 s and 10 s timers, one end answering a door call, an end whose last subscription dropped, a leg the
hub reports `lost`, a frame channel that closes, or the hub's process going away.

**It is inert twice over.** Nothing here runs unless a
registered provider answers `house_bridge_supported()` true (`contract/provider_v1.md` section 4b: the door side's hub on
wheel 0.6.0 started with `-house-calls`, which has its own option). With the 0.5.x wheel the second never holds: the command
answers `not_supported`, a call is the 3a call and no new IPC verb is ever sent. `portico/house/media_open` is `admin, phone,
dashboard` like the other four house commands. `API_VERSION` stays 1: the bridge methods are optional. Tested with fakes
(`tests/test_house_bridge.py`, `test_house_bridge_ws.py`); **no real audio has been carried by any of this.**

## The gate as a roster endpoint and call target (M7 3f, inert)

`gate_target.py`. The gate is the door whose driver reports `CanDial` (a gate_sip_server entry with `dial_enabled` on and its station registered). It is already a roster endpoint of kind `door` (id `<provider>:<device_id>`, name from the door); 3f makes it
callable **only while `CanDial` is exactly true**. Then its roster entry carries `can_dial: true` (live state, like
`available`: not part of the document `rev`, as Talk's availability never is), the editor's endpoint row carries
`can_dial: true` and the house picker lists it (the call-target picker offers a door only with it; a save is not
tightened), the house roster `rev` includes it, and a press of a `call` button that targets it, or of its
`r-...` roster key, goes through Talk's path (`ContentStore._run_talk`): the same gate, the single `provider.dial`, and
the `talk_state` frames to the placing phone only. Only a phone can place it; a screen sees it off (`phone_only`).
With the dial off nothing is different: no new key, a stored reference is kept and shows "not available", a new
reference is only not offered, and a press dials nothing (`tests/test_gate_off_golden.py` compares against a recording made
before 3f). `GateWatch` tells the clients (`house_roster_changed`, when house calls are on) when the set of dialable
doors changes. gate-sip, the Go transport and the old app are untouched.

## How doors reach the registry

The seam is a wire, not a shared module. `async_setup` creates the door side's one provider
(`door/provider_impl.py`) and registers it with the `ProviderRegistry` directly. The provider is a
plain object that returns dicts (`doors()`, `call_state()`, `secrets_for()`, ...) and raises
exceptions with a `code` and a `message`; `provider.py` turns the dicts into its own types.

The wire protocol is [`contract/provider_v1.md`](../../contract/provider_v1.md) (API version 1). Before the fold of
the former `portico_intercom` integration into this one the registry discovered providers through
`custom_components/<domain>/portico.py` and refused one with a different API version
(`provider_version_mismatch_<domain>`); both went with the fold, there being one provider. The `Provider`
protocol, `Door.from_dict` and the conformance suites (`tests/provider_conformance.py`,
`tests/door_provider_conformance.py`, `tests/fake_provider.py`) stay as an internal seam.

## Files

| File | Role |
| :--- | :--- |
| `provider.py` | the provider registry |
| `door/` | the door side: `hub.py`, `transport.py`, `door.py`, `call_state.py`, `provider_impl.py`, `gate_sip_listen.py`, `listen_watch.py`, `relay_action.py`, `wheel.py`, `flow.py` (the door steps of the config flow), `drivers/`, `entities/` (call-state sensor, relay lock, camera) |
| `binary_sensor.py`, `camera.py`, `lock.py`, `sensor.py`, `entities/` | the platform shims: HA imports `portico.<platform>`; a door entry gets the door entities, a phone or push entry the sensors in `entities/sensor.py` |
| `config_flow.py`, `diagnostics.py` | ONE config flow (a menu: add a door or the push ring notification) and one diagnostics dispatcher, both chosen by `entry_type` |
| `signaling.py`, `auth.py` | the `portico/*` commands and the who-may-send table |
| `enrollment_flow.py`, `phone_entry.py`, `phone_flow.py`, `ha_dial.py` | enrollment, phone entries, the QR's `ha_dial` rule |
| `tailscale_approve.py` | the "approve in Tailscale" mode and its OAuth-app store |
| `content.py`, `call_endpoints.py`, `endpoints.py` | content profiles, call buttons, the endpoint registry |
| `house.py`, `house_ledger.py`, `house_commands.py` | house calls, the server model (M7 3a; off, no media, no client) |
| `talk_relay.py` | `talk_state` to the phone that placed a Talk call (M7 P3-B; inert: nothing is placed until a door can dial) |
| `push/`, `push_flow.py` | the push ring notification FCM sender and its config flow |
| `away_media.py` | the away call audio frames |
| `www/` | `portico-card.js`, `portico-panel.js` and their Node tests |
| `tests/` | the Python tests (the door side's too) |

## Tests

From the repository root: `scripts/test_offline.sh custom_components/portico` (see
[`docs/development/running-tests.md`](../../docs/development/running-tests.md) for the test venv). The card and panel tests run with `scripts/test_offline.sh --node`.
`tests/test_house_boundary.py` keeps the house-call modules away from the door path.

## YAML configuration files (DEV-824)

`/config/portico/` holds the live configuration as YAML. `portico.export_config` writes the first files (`dry_run` lists them,
`overwrite` is needed to replace files that already exist; it also resyncs the files from live). From then on every editor save of the
pages, elements or push settings rewrites the files that differ (no diff, no write) and fires the event `portico_config_changed`
(`changed`, `added`, `removed`, `pending`, `source`, `doc_rev`; paths only). A file edited by hand and not yet reloaded is left as
written and listed in `pending` until `portico.reload_config` adopts it. `portico.reload_config` never writes files or fires the event.
Writing happens only while the directory exists and the settings entry's option `config_files` is not `false` (default on).
Secrets and `open_relay_action` are never written.
