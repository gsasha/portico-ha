# Live video of a camera tile

WebRTC live video on camera tiles: the `portico/content/live` commands, the away route (`live_rtsp`), stream sizes, and how the card and the editor use them.

Related: [content, pages and actions](content-pages-and-actions.md), [dashboard card and panel](dashboard-card-and-panel.md).

## Live video of a camera tile (`portico/content/live`, `live_ice`, `live_close`)

A camera in a block may carry `live: true`. The flag is **optional and additive**: absent means false, it is stored and sent
only when true (so a document without it is unchanged on the wire), it is checked as `size` is (a save refuses a value that is
not a boolean, a load reads an unknown value as absent and notes it), and it is for cameras only. It travels with the camera
item in `content/get`, `content/document` and `content/preview`, and changing it changes `rev`. It tells a client which tiles
to start live; the server does not look at it when a client asks for a stream (the grant is the camera, not the flag).

Live video is WebRTC, **video only** (the offer has one receive-only video line and no audio). The card sends its offer over
the Portico websocket and the server hands it to Home Assistant's own camera WebRTC support (go2rtc), in this process, with the
same calls Home Assistant's `camera/webrtc/offer` command makes: nothing else reaches the camera, and no entity id, stream
address or token ever reaches the page.

```
-> {id, type: "portico/content/live", camera_id, sdp, size?: "big" | "small" (default "big"), endpoint?}
<- {id, type: "result", success: true, result: {session_id, stream: "main" | "sub"}}
<- {id, type: "event", event: {type: "session", session_id, stream: "main" | "sub"}}   (always the FIRST event)
<- {id, type: "event", event: {type: "answer", sdp}}
<- {id, type: "event", event: {type: "candidate", candidate: {candidate, sdpMid?, sdpMLineIndex?, ...}}}   (zero or more)
<- {id, type: "event", event: {type: "error", code: "live_unavailable", message}}                        (then the session is closed)

-> {id, type: "portico/content/live_ice", session_id, candidate: {candidate, sdpMid?, sdpMLineIndex?, usernameFragment?}}
<- {id, type: "result", success: true, result: {}}          (an empty `candidate` is the end-of-candidates mark)

-> {id, type: "portico/content/live_close", session_id}
<- {id, type: "result", success: true, result: {closed: true | false}}      (false: nothing was open; never an error)
```

- **The grant is `content/snapshot`'s**, one function for both: the caller class and the endpoint come from `_content_target`
  (a phone its own endpoint, a declared screen its own, an administrator names one with `endpoint`), the camera is the id in
  that endpoint's resolved document (`unknown_camera` otherwise, written to the logbook as `refused a live view of ...`), then
  the same `unavailable` test. All three commands are `admin, phone, dashboard` in the auth table. `live_ice` and `live_close`
  work only on a session **the same websocket opened**: anything else is `unknown_session` (`live_close` answers `closed: false`).
- **The session event.** Home Assistant's client library (`subscribeMessage`) resolves with the unsubscribe function and drops
  the `result` frame, so the card takes `session_id` from the first event, `{type: "session", session_id, stream}`, which is
  sent right after the result and before any `answer`, `candidate` or `error` event (as Home Assistant's own
  `camera/webrtc/offer` does). A request refused before the result has no session and no session event; an offer that fails
  afterwards is that session event followed by the `error` event.
- **Errors before the result** (an error frame): `unauthorized`, `unknown_endpoint`, `unknown_camera`, `unavailable`,
  `too_many_live`, `invalid_format` (the offer is at most 16 KB, `size` is `big` or `small`), and **`live_unavailable`**:
  Home Assistant has no WebRTC for that camera (it is off, is not there, has no stream or no go2rtc provider). **After the
  result**, a failure (the camera's handler raised, or go2rtc reported an error) is the `error` event above with the same
  code; its text is fixed and never carries Home Assistant's. On either, **the card stays on stills, which are never removed**.
- **`live_rtsp`: the stream hint for the away route (DEV-713 S1b-1).** Away from home the phone app cannot use Home Assistant's
  WebRTC; it pulls the camera's go2rtc stream over RTSP through its tailnet. To know WHICH stream belongs to a tile, a camera item
  of a resolved document may carry, in `content/get`, `content/document` and `content/preview` (everywhere `live` rides):

  ```
  "live_rtsp": {"main": "rtsp://192.168.2.6:8554/front_door", "sub": "rtsp://192.168.2.6:8554/front_door_sub"}
  ```

  `main` is always there when the object is; `sub` only for a camera named in `live_sub_streams`. The app uses `main` for a Big
  tile and `sub` (else `main`) for a Small one. The object is **computed per request and never stored** (a document that carries
  one has it dropped by the validator and the save), is **never an entity id**, and is present only when ALL of these hold: the
  settings entry's `live_rtsp_base` is set (default empty = the feature is off and no document carries a `live_rtsp` anywhere);
  the camera item is in the endpoint's own resolved document (the same grant as `live`); it has `live: true`; and its entity is a
  Frigate camera (its state has the `camera_name` and `client_id` attributes the Frigate integration sets; the stream name is
  that `camera_name`, as go2rtc streams in Frigate are named after the camera). The last name seen is remembered, so a camera
  that blinks to `unavailable` does not move `rev`. It **is** part of `rev`, so changing either option moves every `rev` and
  announces a `content_changed` refetch. A document and a configuration without the options are byte-for-byte what they were,
  and older apps ignore the key. No new command: `contract/app_commands_v3.txt` is unchanged.
  **On the caller's cameras (DEV-713 S2-1).** The cameras the ring and in-call screens show, `callers.<id>.cameras[]` of
  `content/get` (ids `c-<12 hex>`; the push ring payload carries no camera list), carry the same `live_rtsp` object on the same
  terms, with one difference: they have no per-camera `live` flag, so **`live: true` is not required**. The app decides (it shows
  the FIRST caller camera live by default); the hint only says how to reach it away from home. Still required: `live_rtsp_base`
  set, a Frigate camera entity (`camera_name` and `client_id`), `sub` only for a name in `live_sub_streams`, never an entity id,
  never stored, part of `rev`. It appears only on the cameras of the callers the endpoint's own document lists (never on the
  endpoint's own entry, never under the unknown caller `"*"`). `portico/content/live` and `content/snapshot` accept exactly the
  `c-` ids listed there (one grant); an id that is not listed is `unknown_camera`. At home the app still uses WebRTC with the id.
  The two options are `live_rtsp_base` (`rtsp://host[:port]` with an IPv4 or hostname, at most 128 characters; credentials, a
  path, a query, IPv6 and other schemes are refused) and `live_sub_streams` (Frigate camera names, `[A-Za-z0-9_-]{1,64}`, at
  most 64, each with a go2rtc stream called `<name>_sub`). They are edited on the [Settings tab](settings-tab.md).
  **Security:** the RTSP port is outside Portico's per-camera grant. Anyone with a Portico phone on your tailnet that is allowed to
  reach that port can pull ANY stream on it, not only the cameras shown to them (design Q7: accepted).
- **`streams`: the stream sizes of a camera tile (additive).** A camera item of a resolved document (`content/get`,
  `content/document`, `content/preview`; the block cameras, not the callers') may carry the live picture size of its two
  variants, so the app can name a stream by its resolution ("1440p") before it has played:

  ```
  "streams": {"big": {"width": 2560, "height": 1440}, "small": {"width": 640, "height": 360}}
  ```

  `big` is the size of the element's main entity, `small` of its small stream entity (`small: true`); each key only when
  known, and no `streams` key at all when neither is. Source and cost: the entity's own `width`/`height` attributes, else the
  RTSP DESCRIBE of `camera_stream_size.py` through `CameraInfo`'s 10-minute cache (the same one the Pages editor uses); a
  document build NEVER probes, it reads what was learned. The first build after a restart therefore has no `streams`; it
  starts one background refresh per camera (at most one per 10 minutes), and when a size arrives or changes `content_changed`
  announces a refetch (the sizes are part of `rev`). A camera that is down or answers nothing keeps its last known size.
  Computed per request and never stored (a stored `streams` is ignored). No contract bump: `contract/app_commands_v3.txt` and
  the content `v` are unchanged, and an older app ignores the key. The small entity id still never leaves the server.
- **No product cap.** A very high technical guard stops a runaway client: 16 live sessions per endpoint and 32 per install,
  then `too_many_live`. A refused request takes no slot. The editor's "many live tiles" warning is the card's and the editor's
  business, not this command's.
- **A session has no expiry.** It lives until `live_close`, `unsubscribe_events` for the `live` subscription id, or the
  websocket closing; each tells Home Assistant's camera to let go of it (also when the offer was still in flight). There is
  nothing to renew and nothing to leak while the websocket is open: a vanished client closes everything it opened.
- **`size` is the stream variant (DEV-768).** `big` takes the camera's main stream (the element's `entity_id`). `small`
  takes the element's own **`options.small_entity_id`** (a second camera entity, set in the Elements editor as "Small
  stream (optional)"); a resolved camera item says `small: true` when the element has one (its id never leaves the server),
  and `live_rtsp.sub` is then given for a Frigate camera without being listed in `live_sub_streams`. An element with one
  stream always shows it. The phone, not the server, decides the variant; an older app that sends the tile size gets the
  same words and the same behaviour. Without an element small stream, `small` falls back to the install option below:
  Home Assistant's camera entity is one stream, so the pair is an explicit lookup and never a guess. The settings entry's
  option `live_sub_cameras` (`{"camera.gate_intercom": "camera.<its sub stream entity>"}`, default empty) names the sub
  entity of a main one; with no entry for a camera, or a sub entity that is unavailable or has no WebRTC, both sizes use the
  main stream. The result's `stream` says which was used. The option is read from the entry's options when a session starts;
  there is no setting for it in the panel yet.
- Candidates and the answer use the keys of the browser's `RTCIceCandidateInit` and session description, so the card can
  pass them straight to `RTCPeerConnection` (a server candidate may omit `sdpMLineIndex` when it names `sdpMid`).

### Live tiles on the card, and the Live checkbox in the editor (live video, part 2)

The card (`www/portico-card.js`: the kiosk page and the dashboard card) plays the stream the three commands above serve; the
editor (`www/portico-panel.js`) sets which tiles do. The three commands are listed in `contract/app_commands_v3.txt`, so they are available to the phone app; the app's own behaviour is not described here.

- **Which tiles play.** A camera tile of a block with `live: true` plays WebRTC video in a `<video muted playsinline autoplay>`
  over its still. The still stays underneath as the poster and is fetched as before until the first frame plays; a tile that
  plays asks for no stills. The size hint comes from the tile as drawn: a **Big** tile asks for `big`, a **Small** one (and a
  wide layout's side or thumb tile) for `small`. The offer is one receive-only video line, no audio line, no microphone, in a
  peer connection of its own: the call's connection and its `<audio>` element are never touched, and a call answered while a tile
  plays changes nothing about the tile.
- **A tap switches a tile** between live and still, for this viewer. The choice is remembered per browser and per screen
  (`localStorage`, in a try/catch; with the storage blocked it lasts until the page closes). Only a choice that differs from the
  document is stored: tapping back to the document's setting forgets it, so a later change of the document reaches a viewer who
  has not chosen. A tile that should be live but gave up (see below) answers a tap by asking again, not by switching off.
- **Off screen, nothing streams.** An `IntersectionObserver` (threshold 0) watches every live tile: a tile **completely** off
  screen has its session closed and reopens when it intersects. A tile is not streamed until the observer has reported it. A
  page that is hidden (`visibilitychange`) closes every session and reopens them when visible again. While the ring overlay is
  up, the page tiles behind it are let go. A tile that leaves (another page tab, a layout that gives it another size, `live`
  taken off in the editor) is closed. There is **no idle drop**: a session has no expiry.
- **Sessions are keyed by camera and size** (`page:<camera>|<size>`, `ring:<camera>|big`), held on the card and not in any
  rendered subtree. The card re-renders by replacing `innerHTML`; after each render it finds the tiles again and moves each
  session's own `<video>` element into the new tile, so a re-render opens nothing, closes nothing and does not restart the
  picture. Every session ends with one `live_close` (and the websocket unsubscribe) when its tile goes, the card is removed or the
  page unloads (`pagehide`).
- **A ring** brings the caller's first camera (the `c-` id the overlay already gets) live by default, `big`, with the same
  fallback, and closes it with the overlay. The overlay's other cameras stay stills.
- **What a viewer sees on a failure: the stills, and no text.** Any error answer to the command (`unauthorized`,
  `unknown_endpoint`, `unknown_camera`, `unavailable`, `too_many_live`, `invalid_format`, `live_unavailable`, or a server
  that does not know the command) is final: no retry. An `error` event, a peer connection that fails or closes, an ICE
  disconnect that does not recover in 5 s, a lost connection while asking, or no first frame in 20 s is retried after 2 s, 5 s and
  15 s (the count starts again after a good run), then the tile stays on stills until a tap or the next visibility change.
- **How the card gets the session id.** Home Assistant's websocket library resolves a subscription with only its unsubscribe
  function and drops the `result` frame that carries `session_id`. The card reads that frame as it passes on `connection.socket`
  (matching the command id the library writes into the message), and also accepts a first event `{type: "session",
  session_id}` should the server send one. With neither, the video still connects (the answer and the server's candidates need
  no id) but the card's own candidates cannot be sent and the unsubscribe alone ends the session on the server.
- **Show frame rate.** `show_fps` is a document-level boolean beside `home_name`: optional and additive, stored and sent
  only when true (a document without it is unchanged on the wire), refused by a save when it is not a boolean, read
  leniently on a load (anything else is false and noted), and carried on the resolved document (`content/get`, `document`,
  `preview`), where it changes `rev`. With it on, each live tile draws a small corner label: `--` until known, then `24 fps`,
  measured about once a second from `requestVideoFrameCallback` where the WebView has it, else from `getStats()`
  (`framesPerSecond`). A stalled picture reads `0 fps`.
- **The editor.** Every camera tile of a block has a **Live** checkbox (and the add and edit forms have one): the tile's
  setting, whether or not there is a call. It stores `live: true` only when ticked and removes the key when not. The preview
  marks live tiles `LIVE`. When more than 2 tiles of one page are live the page shows a warning ("Several live pictures can
  overload the tablet or the camera server; turn on Show frame rate to see whether a picture lags, and take cameras off live
  if it does"); it is advice and never blocks a save or caps anything. Every editor view (the template, a phone, a screen) has a **Show the frame rate on live
  pictures** checkbox for `show_fps`; it is a whole-household setting, and a phone's or screen's view says so under the box. The panel's validation mirrors the server's: `live` and `show_fps` must be booleans.
- **Tests.** `www/tests/test_card_live.mjs` and `test_panel_live.mjs` (jsdom: websocket, `RTCPeerConnection`,
  `IntersectionObserver`, `requestVideoFrameCallback` and the `<video>` element are fakes) and `tests/test_show_fps.py`. They prove the
  card's logic. **Not verified in a real browser or on a tablet:** that a WebView plays the video, the look of the tile and the
  label, the time to first frame, and how the picture behaves when a `<video>` element moves between renders.
