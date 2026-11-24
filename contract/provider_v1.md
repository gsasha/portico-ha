# Portico provider protocol, version 1

The seam between the Portico provider registry (`custom_components/portico/provider.py`: the websocket commands, the
push sender, content) and whatever supplies doors. Requirements PROV-01..07, DRIVER-02, TEST-06. **Since the fold of the
former `portico_intercom` integration into `portico` (DEV-686) there is one provider, the door side's
(`custom_components/portico/door/provider_impl.py`), and this protocol is an internal seam**, kept as it was: **the seam
is a wire, not a shared module**. A provider returns plain dicts and raises exceptions that carry `code` and `message`;
the registry (`provider.py`) turns the dicts into its own dataclasses. `provider.py` is the reference reader of this
document.

`API_VERSION` is `1`. It changes only when a dict key, a method or a rule below changes incompatibly. A new
**optional** method (sections 4a and 4b) is not such a change: the registry looks for it with `getattr`, so a provider
without it keeps working. (The house-call design planned `API_VERSION 2` for the bridge methods; they were built as
optional methods instead.) Nothing checks the version at runtime any more (see section 1).

## 1. Registration (PROV-01)

Before the fold a driver integration shipped `custom_components/<its_domain>/portico.py` with
`async_register_provider(hass, registry)`, the registry discovered it through
`async_process_integration_platforms`, and `register` refused a provider whose `API_VERSION` differed (the repair issue
`provider_version_mismatch_<domain>`). Those three things are **gone**: they existed for third-party drivers in a separate
integration. Now `async_setup` of the `portico` integration creates the door provider (`install_provider`) and calls
`registry.register(provider)` itself. A second `register` of the same domain replaces the first. It does not fire the
bus event; the door side does (section 6). The provider's `domain` is the door provider id `portico_intercom` (persisted
in endpoint ids and the push `door_ref` hash: it is NOT the HA domain `portico`).

## 2. The provider object

An object with a class or instance attribute `API_VERSION: int` and `domain: str` (the provider's HA
domain: no colon), and these methods. `async` marks coroutines. Every returned dict is plain JSON-like
data; every failure is an exception with `code: str` and `message: str` attributes (a driver's own type
is fine; the UI part reads them with `getattr`, and treats an exception without a `code` as `internal`).

| Method | Returns |
| :--- | :--- |
| `doors()` | `list[door dict]` (section 3) |
| `call_state(door_id)` | `{"door_id", "call_id", "state"}` or `None` for an unknown door; `state` is `idle \| ringing \| answered \| active` (never `ended`: that is a transition); a snapshot that is malformed or carries another state is dropped and logged, and the door gets no subscribe snapshot |
| `subscribe(listener)` | an unsubscribe callable; `listener(transition dict)` per call transition (section 4) |
| `subscribe_outbound(listener)` | **optional** (section 4a). An unsubscribe callable; `listener(outbound dict)` per change of a call Portico placed |
| `subscribe_doors(listener)` | an unsubscribe callable; `listener()` when a door is added, removed or changes availability |
| `secrets_for(door_id, phone_id)` | the credentials a phone needs for that door, or `None` for an unknown phone or door (PROV-07) |
| `async answer(door_id, call_id, *, client_kind, claimant, host="", port=0)` | answer dict (section 5) |
| `async dial(door_id, host, port)` | dict; carries `call_id` when a call was placed (a dialled call has no ring and no transition) |
| `async hangup(call_id, reason)`, `async open_relay(door_id)`, `async keepalive(call_id)` | dict (the transport's result, passed through) |
| `async webrtc_offer(call_id, sdp)`, `async webrtc_answer(call_id, sdp)`, `async webrtc_ice(call_id, candidate)` | dict |
| `async media_open(call_id, codec, on_frame, on_closed)`, `media_frame(call_id, data)`, `async media_close(call_id, reason)` | dict / `None` |

`subscribe_doors` ordering rules (each has a case in the conformance suite):

- On unload the provider **detaches first, then notifies**, so `doors()` already lacks the door when a
  listener re-reads it. On setup it **attaches first, then notifies**.
- Listeners run on the event loop.
- A listener that raises is logged and skipped; the others still run.
- After `unregister()` the registry ignores any late notification from that provider.

Subscribing twice gives two independent subscriptions; calling an unsubscribe twice is harmless.

## 3. The door dict

```json
{"id": "http://10.6.0.7:8091", "provider": "portico_intercom", "name": "Gate", "driver": "gate_sip_observer",
 "capabilities": {}, "camera_entity_id": "camera.gate", "available": true, "away_media": false}
```

| Key | Type | Meaning |
| :--- | :--- | :--- |
| `id` | str | the driver's stable `device_id` (PROV-03), never an HA `entry_id`; may be a URL |
| `provider` | str | the provider's domain (the registry uses the domain it registered, not this key) |
| `name` | str | display name |
| `driver` | str | driver key, e.g. `dahua_sip`, `unifi_protect`, `gate_sip_observer`, or a third party's own |
| `capabilities` | object | the driver's capabilities, verbatim |
| `camera_entity_id` | str or null | |
| `available` | bool | |
| `away_media` | bool | whether this door can carry the away-media leg (no longer an option: always on; false only for a ring-only door without talk-back); published here so the UI part never reads a driver's config entry |

A missing key or a wrong type makes that one door count as absent (logged); the provider stays
registered. A `doors()` that raises counts as no doors.

**Namespacing (PROV-06, ENDPOINT-02).** The endpoint id of a door is `<provider>:<id>`. A command may
name a door either way. An id is **namespaced if and only if the text before its first colon is a
registered provider domain**; otherwise it is a bare `id`. So `http://10.6.0.7:8091` is bare (`http` is no
provider), and `portico_intercom:http://10.6.0.7:8091` is namespaced. A bare id that two providers both
supply is refused with code `ambiguous_door`. Split on the first colon only.

**`door_ref` (WS-08).** The push `door_ref` is derived, not supplied: `"d-" + sha256(endpoint_id)[:32]`
(hex). It satisfies the push id grammar `^[A-Za-z0-9._:-]{1,128}$` whatever the door id is, and it is a
function of the stable id, so it survives a re-add of the entry.

## 4. The transition dict

```json
{"door_id": "...", "call_id": "c1", "state": "ringing", "prev_state": "idle", "prev_call_id": null, "at_wall": 1760000000.5}
```

`state` and `prev_state` are `idle | ringing | answered | active | ended`; `call_id` and `prev_call_id`
may be null; `at_wall` is epoch seconds. A transition that is missing a key or mistyped is dropped and
logged. When a provider unregisters, or a door leaves a provider that stays registered, the registry
itself tells its subscribers once per door with a transition whose `state` is `unavailable` and `call_id`
null (PROV-05); a provider never sends that state. The calls of a door that left are no longer routable
(`for_call` forgets them), and a door that arrives later, or comes back after an entry reload, is announced
to the UI part's subscribers the same way (its snapshot). The registry diffs `doors()` before and after
each `subscribe_doors` notification, so a provider only has to notify, never to say which door.

## 4a. The outbound transition dict (optional, M7 P3-B)

A provider that can place calls (a door that reports the `CanDial` capability) may also implement
`subscribe_outbound(listener)`. A provider without the method is exactly what it was: the registry looks for it with
`getattr`, so `API_VERSION` stays `1`.

```json
{"door_id": "...", "call_id": "out-0123456789abcdef", "state": "in_call", "prev_state": "dialing", "at_wall": 1760000000.5}
```

`state` is `dialing | in_call | ended`; `prev_state` is one of those or `idle`; `call_id` is the id the provider
minted for the call it placed (the driver part's start with `out-`). A transition that is missing a key or mistyped is
dropped and logged. These are **never** part of `subscribe` (section 2): a call a provider placed is not a visitor ringing,
so the phones' `call_state_changed`, the push, the sensor and the bus event of section 6 never carry it. The only reader
is the UI part's talk relay (`talk_relay.py`), which sends `{"type": "talk_state", ...}` on `portico/calls/subscribe`
to the phone that pressed a Talk button and to no other client (the wire is in `contract/ws_v3.json`).

## 4b. The house-call bridge (optional, M7 3b-S)

A provider whose hub can join the two legs of a phone-to-phone call (the driver part with wheel 0.6.0 started with
`-house-calls`) may implement these. A provider without them is exactly what it was, `API_VERSION` stays `1`, and every
house-call media path in the UI part answers `not_supported`. A house call id (`hso-...`) is **not** a door call id: it never
reaches `answer`, `hangup`, `keepalive`, `webrtc_*` or `media_*` above, and a provider must not put one in its call tables.

| Method | Meaning |
| :--- | :--- |
| `house_bridge_supported() -> bool` | **Feature detection**, asked every time: can this provider bridge a house call right now? False (or raising) means no. A provider whose hub is a 0.5.x wheel, or has the option off, answers False |
| `async house_open(call_id, legs) -> {session, legs}` | open one bridge with exactly two legs: `[{side: "from"\|"to", kind: "frames"\|"webrtc", codec: "pcmu"\|"l16" (frames) \| sdp: <offer> (webrtc), owner?: <endpoint id>}]`. The reply describes each leg: `{side, kind, state, leg_id, codec, frame_bytes, frame_ms}` for a frame channel (`leg_id` starts `house-`), `{side, kind, state: "pending"}` for a WebRTC leg whose answer follows as a leg state. Failures carry a `code` (`not_supported`, `bad_params`, `already_open`, `too_many_calls`, `shutting_down`, `house_open_failed`) |
| `async house_close(call_id)` | close the bridge; idempotent |
| `subscribe_house(listener)` | an unsubscribe callable; `listener({call_id, leg, state, reason?, sdp?})` per leg state: `leg` is `from`, `to` or `both`; `state` is `attached`, `first_frame`, `lost` or `sdp` (`sdp` carries the WebRTC answer). A provider whose process goes away sends `lost` with `leg: "both"` once per bridge it opened. A malformed report is dropped and logged |
| `house_media_open(leg_id, on_frame, on_closed)` | register the callbacks of a frame-channel leg the bridge made: downlink frames reach `on_frame(base64)`, the leg ending on its own reaches `on_closed(reason)` once. No request is sent |
| `house_media_frame(leg_id, data)` | one uplink frame for that leg, fire and forget |
| `async house_media_close(leg_id, reason)` | the end's goodbye on its leg |

## 5. The answer dict (STATE-02)

`{"verdict": "answered" | "taken_elsewhere" | "refused" | "not_available", "mode": "direct" | "relay" | null, "detail": {...}}`

One arbitration, inside the provider: for one call at most one claimant gets `answered`, every other
gets `taken_elsewhere`. `refused` is the provider declining (for instance the driver has answering
disabled); `not_available` is an unknown or already ended call or door. `claimant` is the websocket
connection's identity string; `detail` is the transport's result passed through. A provider may instead
raise an exception with a `code` for a failure that is none of these.

## 6. The bus event (PROV-04)

The **door side** fires `portico_intercom_call` on the HA event bus from its own transition fan-out, whether or not
any phone is enrolled. The registry's consumers only listen. The name is historical and must not change (automations
trigger on it). `event.data`:

| Key | Meaning |
| :--- | :--- |
| `device_id` | the door's `id` |
| `state` | the new state |
| `call_id` | the call the change is about (on `ended`, the call that ended) |
| `prev_state` | the previous state |
| `prev_call_id` | the previously tracked call id, or null |
| `at_wall` | epoch seconds |

## 7. Conformance (TEST-06)

`custom_components/portico/tests/provider_conformance.py` is the suite any provider runs; it checks the
version attribute, the `doors()` shape, subscribe and unsubscribe symmetry (calls and doors), the
`subscribe_doors` ordering rules above, the answer verdict set and exclusivity, `secrets_for` returning
`None` for an unknown phone, and `doors()` emptying on unload. A driver part copies the suite and the
fake (`tests/fake_provider.py`) into its own `tests/` rather than importing them.
