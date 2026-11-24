# The transport process

The one `portico-transport` process that serves a whole Home Assistant instance: the roles it plays, and the IPC verbs and notifications between it and the integration.

Up: [architecture overview](overview.md). The IPC contract is `contract/transport_ipc_v1.json`; see [contract fixtures](../reference/contract-fixtures.md).

## What the one transport process serves

One `portico-transport` process serves a whole HA instance. Its media roles:

1. the configured drivers (`dahua_sip`, `gate_sip_server`, `unifi_protect`), each registered under
   its `device_id` and addressed by it;
2. the browser WebRTC bridge (the SFU, keyed by call id);
3. the away-media frame leg (`media_open` / `media_frame` / `media_close`, keyed by call id).
4. the house-call bridge (`transport-rs/src/house/`, M7 3b): two legs of one phone-to-phone call joined in
   the hub, in a table of its own (never the door tables), with per-leg counters logged as one line when the call ends (3g). **Off unless the binary is started with
   `-house-calls`**; off, `house_open` and `house_close` answer `not_supported` and nothing is created. The driver
   part (M7 3b-S) passes the flag **only** when the installed wheel is 0.6.0 or newer **and** a door entry's option
   `house_calls` is exactly true (default off; a door's Reconfigure form writes it, M7 3g, and shows it only with such a wheel); a 0.5.x binary exits on a flag it does not know,
   so it never receives one. On:
   `house_open {call_id: "hso-...", legs: [{side: from|to, kind: frames|webrtc, codec | sdp, owner?}, ...]}` returns
   `{session: "house-<hex>", legs}` before any ICE gathering (a WebRTC answer arrives as `house_leg_state
   {state: "sdp"}`), `house_close {call_id}`, and the notification `house_leg_state {call_id, leg, state:
   attached|first_frame|lost|sdp}`. A frame-channel leg is driven with the existing `media_frame` / `media_close` /
   `media_closed` shapes under its `leg_id` (ids start `house-`; every other id still goes to the door path).
   Bounds: 4 bridges, 2 legs per named owner, a leg silent for 10 s ends the call, the hub's 5 s uplink idle rule
   is kept for frame-channel legs.

IPC verbs: `configure`, `deconfigure`, `dial`, `answer`, `open_relay`, `stream_url`, `hangup`,
`keepalive`, `webrtc_offer`, `webrtc_answer`, `webrtc_ice`, `media_open`, `media_frame`,
`media_close` (and `house_open` / `house_close`, off by default). `deconfigure {device_id}` stops one driver and leaves the process and the other doors
running; `configure` of a `device_id` already present answers `already_configured`. Notifications
carry the door: `ringing {call_id, device_id}`, `call_state_changed {call_id, device_id, state}`
(`device_id` is empty for a transition of a call the process never saw ring or dial),
`driver_error {device_id, message, code}`, plus `driver_availability`, `listen_state`,
`media_frame`, `media_closed` (and `house_leg_state` from a hub running with `-house-calls`). Call counters (DEV-811 P3, off unless asked): a `gate_sip_server` configured with `call_stats: true` sends `call_audio_stats {device_id, call_id, ...}` once per answered call at the end of its audio, and a `media_open` with `stats: true` gets a `stats` object in its `media_closed`; the driver's `Capabilities` then carry `Features: ["call_stats"]`, which is what the integration tests before asking (never a wheel version). Wheels before 0.7.3 ignore both keys.
