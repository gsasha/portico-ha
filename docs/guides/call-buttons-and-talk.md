# Call buttons and Talk to the door

Buttons that place a call: `call` buttons aimed at a phone, dashboard or door, the Talk button, the gate path, and the `talk_state` events that follow the call.

Related: [content, pages and actions](content-pages-and-actions.md).

## Call buttons (house calls design section 14, steps 14a/14b)

A `call` action is a button that rings a **target**: `{"id", "label", "kind": "call", "icon"?, "target": {"type":
"dashboard"|"door"|"phone", "id": "<endpoint id>"}}`. `confirm` is **always `false`** for a call: there is no
"are you sure" step to call anyone, the editor does not offer the question and a `confirm: true` sent or
stored is ignored. The phone never sends an endpoint id: pressing the button is the ordinary
`portico/content/run {action_id}`, and the plugin reads the target from the phone's own stored profile
(the profile is the grant).

Who can be called is decided by a **call-endpoint registry** (`call_endpoints.py`, a small interface:
`endpoints(caller_user_id)` for the editor, `target_state(type, id)` for `content/get`, and
`place(caller_user_id, caller_phone, target_type, target_id)` for `content/run`). The registry that ships
**knows no endpoint and places nothing**, so on an install with no calling back end: the editor
offers no one to call, an existing call button shows off, and a press is
refused with `not_available`. A calling back end (not part of this repository yet) registers a real registry with
`set_call_registry(hass, registry)`. Saving refuses a target the registry does not know (a button already in
the profile keeps its target even if it vanished, so other edits still save).

- `portico/content/get` (a page's actions): a call action carries `target`, `name` (the endpoint's current name), `available`,
  `reason` and the same three as `target_state: {label, available, reason}`. `reason` is `null` when
  available (except `push_only`), else one of `not_available` (calls are not set up on this install), `unknown_target`
  (removed), `offline`, `push_only` (the one reason an AVAILABLE target carries: callable, rings by notification; M7 3c), `dnd`, `busy`, `receive_off`, `disabled`.
- `portico/content/run`: success is `{"ran": true, "call_id": "hso-...", "media": {...}}` (`media` is the
  caller's leg descriptor, set by the registry); a refusal is an error frame whose `code` is the registry's:
  `not_available` (default registry), `rate_limited` (once per 2 s per action), `unknown_action`, `failed`
  (the registry raised), or any of `not_callable`, `target_offline`, `target_dnd`, `busy`, `declined`,
  `no_answer`, `dial_disabled`, `gate_busy`, `yielded`, `place_forbidden`. Nothing is queued when a call
  cannot be placed. The call's progress after that arrives as `house_call` events (not built yet).
- The logbook line is `Portico: <phone>` with `ran "<label>" (rang)` or `refused "<label>" (<code>)`.

`portico/content/snapshot {camera_id, width?}` returns one still from a camera **in the caller's own
profile** as `{content_type, content}` (base64), over the same WebSocket, so it works over the
tailnet too. `width` is 64 to 1920 (default 640); a picture over 1.5 MB is refused; a camera can be
asked once a second per phone. The app polls it every few seconds while its home screen is open, so a
successful snapshot is not written to the logbook (a refusal is). The frames of all the content
commands, as the real handlers emit them, are in `contract/ws_v3.json` (the app replays them).

## Talk to the door

**Talk to the door (server gate, switched off).** A `talk` button acts on its block's `door` and has no target of its
own. Its state is `{available, reason}`, computed by `ContentStore.talk_state` in this order: `phone_only` (the document
belongs to a screen, not a phone), `no_door` (the block has no door, or it is not in `portico/doors`), then, for a call
this phone started and that is still going, `connecting` / `in_call` (available: a second press hangs it up), then
`talk_off` (the door's capabilities do not say `CanDial` exactly `true`: **every door today**), `not_connected` (the door
is down), `busy` (a visitor call, an outbound call that is not this phone's, or a dial in flight); otherwise ready
(`reason: null`). `portico/content/run` of a Talk id (the id is in the grant) is refused `not_allowed` unless the caller
is the phone endpoint's own login (a screen, and an administrator naming a phone, are not), then `no_door`,
`not_available` ("Talk to the door is switched off"), `not_connected`, `busy`; only when all of that holds does it call
the door's provider `dial("", 0)` once, and a dial result without a string `call_id` is `failed`. It replies
`{ran, call_id, media: null}`; a press while the phone's own call is going replies `{ran, call_id, hung_up: true}` after
`hangup(call_id, "hangup")`. Only the gate_sip_server driver and the front-door talk driver can report `CanDial` (the Go capability field, `omitempty`, so no
other driver's reply changes), the server only with its transport option `dial_enabled` on and its station registered. The option is
the **Talk to the gate from Portico** switch of the server entry's Reconfigure form; until it is on, nothing is dialled and the button stays off. No websocket
command was added.

**The editor can create a Talk button (DEV-692 T1).** The Portico panel's "What it does" list offers "Talk to the door
(greyed on the phone until the gate allows it)". It adds `{id, label, kind: "talk", confirm: false, icon?, color?,
show_label?}` to a block, takes no target (it talks to the block's own door, so the block must have one: otherwise "Pick
the block's door first"), offers no "Requires confirmation before it runs" and writes `confirm: false` whatever is asked, needs an icon when
its text is hidden, and is never offered to (and is refused in) a ring rule. The editor row and the preview show why it is
off (`Talk is off: Off until the gate allows it`). Saving one changes nothing on the gate: the press stays refused
`not_available` and dials nothing while no door reports `CanDial`. There is no per-door allow list: any enrolled phone's
own login may press it (decision D3 of 2026-10-23); the only identity rule is that the acting login must be that phone's own
HA user.

**A `call` button aimed at the gate: what the app can rely on (DEV-692 T2).** A `call`-kind button whose `target` is
`{type: "door", id: <the gate's door endpoint id>}` is accepted, saved and kept, and when the gate's door reports `CanDial`
(and the placement registry does not know that target) `content/run` of it takes Talk's path (`_run_talk`; the one `dial`),
so it is the same call, the same gate and the same `talk_state` events as a Talk button, and one call is shared between the
two buttons.
- *Before pressing.* The resolved action carries `kind: "call"`, `target: {type: "door", id}`, `available`, `reason` and
  `target_state: {label, available, reason}`. The gate shows `available: true, reason: null` when ready; otherwise
  `available: false` with `phone_only` (a screen, not a phone), `unknown_target` (door removed), `offline` (door down), `busy`
  (a visitor call, another outbound call, a dial in flight) or, with the dial off (`CanDial` absent: the Reconfigure switch
  off), `not_available`. The same state is the gate's `portico/content/get` `roster` entry for the
  target id, which carries `can_dial: true` ONLY while the door reports `CanDial` (the key is absent otherwise).
  **The action says it itself (DEV-695).** The resolved action carries `gate_call: true` exactly when a press of it goes
  through Talk's path: its `target` is a door, the placement registry does not own that target, and the door reports
  `CanDial` now (the very test `content/run` makes, `_gate_door_id`, so the marker and the press cannot disagree). The key is
  ABSENT otherwise (never `false`): with the dial off, for a phone, screen or house target, and for a door the placement
  registry owns. It does not depend on the roster, so a phone whose document hides the gate (`roster_hide`, or a template
  `roster` list without it) still reads it. It says which route a press takes, not whether it can be placed now: it is present
  on a greyed gate button too (`phone_only`, `offline`, `busy`), and for every viewer. It adds no id (the `target` is the
  one the action already had) and is live state like `can_dial`: never part of `rev`, it appears and disappears with
  `CanDial` without a refetch. **What an app does:** read `gate_call` first; if the key is absent, fall back to
  `target.type == "door"` and `roster[].can_dial` for that target id (an older server never sends the key). An older app
  ignores the extra key. With the dial off there is nothing to tell and a press is refused. A press while dial is off is
  refused `not_available` ("this Home Assistant cannot place calls yet"), never `ran`.
- *The reply.* Success: `{ran: true, call_id: "out-<hex>", media: null}`, sent only after the gate answered (a dial blocks
  until then, up to gate-sip's wait), so the `talk_state` events below normally arrive BEFORE it. A second press while this
  phone's own call is going hangs it up and replies `{ran: true, call_id, hung_up: true}`. Refusals are the Talk codes:
  `not_allowed` (not the phone's own login), `no_door`, `not_available`, `not_connected`, `busy`, `rate_limited`, or the
  provider's own dial code (`failed` when the dial returned no call id). `media` is always `null`: the audio is attached
  afterwards by the app's `webrtc_offer` against the `out-` id, as for a Talk press.
- *Following the call.* On the stream the phone already has open, `portico/calls/subscribe` (only the placing phone's own
  streams; no other client, no push, no `call_state_changed`), as `talk_state` events: `dialing` (`call_id: null`, as the
  dial is placed), `in_call` (the `out-` id), `ended` (the id, or null if the dial itself failed; `reason`, only when there is
  one, one of `busy`, `not_connected`, `failed`, `not_available`, `no_answer`, `timeout`). Recorded in `contract/ws_v3.json`
  ("content run: the gate's roster key dials through Talk's path"). If the phone has no stream open for 15 s the server hangs
  the call up; gate-sip ends it at 300 s from the answer.

**Following the call: `talk_state` (P3-B).** The stream a phone already has open, `portico/calls/subscribe`, carries one more
event type for the call that phone placed: `{"type": "talk_state", "door_id", "call_id", "state", "reason"?}`. `dialing`
is sent as the dial is placed (`call_id` is null: the door's reply, and with it the id, comes only once the door has
answered); `in_call` and `ended` follow with the call's `out-` id (the events can arrive before the `content/run` reply that
carries the same id); `ended` carries no `reason` when the call simply ended (a hang-up, the door's end), and otherwise one of the
reasons portico-app maps, nothing else: `busy`, `not_connected`, `failed`, `not_available`, `no_answer`, `timeout` (the dial's error
code mapped to these, `failed` for anything else; a failed dial or a cancelled one has `call_id` null, so the app, which reads only
`out-` ids, learns it from the `content/run` error). It goes only to the phone that pressed (its own HA user's streams), never to another phone,
a screen or an administrator, never to the push path, and it is not a `call_state_changed`: the door's sensor, the bus
event and a wall-screen takeover never see an outbound call. A phone that closes its last stream mid-call has the call hung
up after a 15 s grace. Older apps and the card ignore the event type. The code is `talk_relay.py`; it holds nothing
until a Talk call is placed. A Talk id in a rule's `ring_buttons` or
`call_buttons` is still refused on save.
