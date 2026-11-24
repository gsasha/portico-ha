# Call-state and content events (contract 3)

The event streams of `portico/calls/subscribe` and `portico/content/subscribe`: order, shapes, door naming and error codes.

## Call-state and content events

`portico/calls/subscribe` sends call-state events; `portico/content/subscribe` sends content events; the two never
share a message shape (WS-04). Every call event is `{"type": "call_state_changed", ...}`, sent in this order:

1. The subscribe result.
2. One **snapshot** per configured door, the state at subscribe time, so a client (a phone woken by a push) can ask
   whether a ring is still live:
   `{"type": "call_state_changed", "snapshot": true, "door_id": "192.168.2.20", "call_id": "c1", "state": "ringing"}`.
   `state` is `idle` (with `call_id: null`), `ringing`, `answered` or `active`.
3. **Live events**, one per applied change:
   `{"type": "call_state_changed", "door_id": "192.168.2.20", "call_id": "c1", "state": "ringing", "prev_state": "idle"}`
   (or `answered`, `active`, or `ended` with `call_id` the call that ended). An end is **one** event: contract 2's
   `idle` echo after `ended` is gone. A door that leaves gets a snapshot with `state: "unavailable"`; a door set up
   later gets its snapshot. The wire carries no `entry_id` and no `devices_changed`. The handler is a synchronous
   callback, so nothing can change between the snapshot and the first live event: none is lost or sent twice. M7
   reserves `{"type": "house_call", ...}` on this stream; a client ignores an event type it does not know.
   M7 P3-B adds `{"type": "talk_state", "door_id", "call_id", "state": "dialing"|"in_call"|"ended", "reason"?}`, sent to the
   phone that pressed a Talk button and to no other client: see [Call buttons and Talk to the door](../guides/call-buttons-and-talk.md).

`portico/content/subscribe {endpoint?}` sends `{"type": "content_changed", "endpoint": "<id>", "rev": "<rev>"}`
when the caller's own endpoint's document changes (a phone or a declared screen; `endpoint` naming another's is
`unauthorized`). An administrator names one endpoint and hears it with its `rev`, or names none and hears every change
with `rev: null`. It never carries a call event.

The commands name a door by `door_id` (`portico/open_relay`, `answer`, `dial`, `webrtc_offer`, `media_open`) and an
unknown door is `unknown_door`. `answer` errors are `already_answered` (the call was taken), `refused` (the door does
not let Portico answer), `not_available` (the call is gone or nothing is ringing) and `unknown_door`. `portico/doors`
lists `{door_id, door_ref, endpoint_id, name, driver, capabilities, camera_entity_id, available, away_media}` and no
secrets. `portico/push/register` refuses an `app_version` below `0.5.0` with `app_too_old`.

## Call events on the Home Assistant bus (`portico_call`)

Every ring and call, of every kind, is a `portico_call` event on the HA event bus, one per phase: `ringing`, `answered`, `active`,
`ended`. A ring is an event even if nobody answers. This is separate from the websocket streams above, and the older
`portico_intercom_call` event and `sensor.call_state` are unchanged.

| Field | Meaning |
|---|---|
| `phase`, `kind`, `direction` | `ringing \| answered \| active \| ended`; `door` (a visitor), `outbound` (a Talk call placed from a phone) or `house` (between two endpoints of the house); `in` or `out` (omitted for a house call) |
| `call_id`, `door_id`, `door_name` | the call id the other records use; the door, for `door` and `outbound` |
| `from`, `to`, `answered_by` | endpoint names as the house shows them (house calls); omitted when unknown |
| `at` | UTC time of the phase |
| on `ended` | `outcome`, `ring_s`, `talk_s` |

`outcome` is `answered`, `missed`, `superseded` (a new ring replaced it), `lost` (nobody reported the end; dropped by the 60-second check), `rang`
(a ring-only door), or, for a house call, the registry's own end reason (`hangup`, `no_answer`, `declined`, `cancelled`, `busy`, `dnd`, `error`). Treat
any other word as unknown. A field that has no source is left out, never `null`. An outbound call has no separate `answered` phase.

`sensor.portico_calls` (`idle`, `ringing`, `talking`) covers every kind of call, and `binary_sensor.portico_call_active` is on while it is not `idle`.
`sensor.portico_live_sessions` (diagnostic, DEV-805) is the number of live video sessions open now; its attributes give the count per endpoint and the two limits. Each open and close is an INFO line in the log naming the endpoint, session, camera and closing reason, and a `too_many_live` refusal lists what the endpoint holds, with ages.
Both belong to the push ring notification (settings) entry. `portico_call_stats` carries audio counters for a call 5 seconds after it ends, when a
transport reports any. One logbook line is written per ended call.

## Door openings from the app (`portico_action`)

When a phone, screen or wall panel opens a door through Portico (`portico/open_relay`, or a `portico_open` element run), one
`portico_action` event is fired on the HA bus **before** the door's action runs. The app builds one user-less HA context, fires
the event with it, and runs the door's action (the installer's `open_relay_action`) with the **same** context, so the logbook
groups the action's own rows (its service call, the gate entity's state change) with the event and shows one readable row,
`Portico: <phone> opened <door>`. The context carries no user, so no permission check changes; the actor's HA user is a field.

| Field | Meaning |
|---|---|
| `action` | `open` |
| `actor`, `actor_user` | the phone or panel name as the house shows it, and its HA user id; omitted when unknown |
| `source` | `app` (a phone or screen) or `panel` (a wall-tablet login) |
| `door_id`, `door_name` | the door |

There is no `result` field and no event for a refused open (a refused open never reaches the action). The event is best effort: if
firing it fails, the door still opens, with a plain context, as before. An element run keeps its own existing logbook line
(`Portico: <phone> ran "<label>"`) unchanged. Not covered yet: answer, hangup, dial and house actions, and the relay lock entity.
Names and door ids only; no tokens, audio or addresses.
