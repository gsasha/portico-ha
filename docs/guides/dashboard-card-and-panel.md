# The dashboard card, the wall tablet and the panel

The Portico page in the sidebar and the `portico-card` Lovelace card: configuration, the ring overlay, the kiosk page, and what they cannot do.

Related: [content, pages and actions](content-pages-and-actions.md), [live video](live-video.md).

## The panel and the card

**Seeing it:** administrators get a **Portico** page in the sidebar. It lists every enrolled phone and
draws its profile the way the app's home screen will: a tile per camera with its live picture
(refreshed every 10 seconds), then a tile per button showing what it does, whether the phone requires confirmation,
and whether its entity is available right now. It only shows; nothing on it runs. It reads
`portico/content/document` (admin only) and updates when a document changes. `www/tests/panel_harness.html`
renders the page with made-up data for a visual check (`firefox --headless --screenshot`).

**Two phones with the same name.** Every enrolment makes a new phone user, so a phone enrolled twice is
listed twice under the same name, and a profile belongs to one of them (the one whose app is signed in).
The editor's dropdown and the panel therefore show, beside the name, the day it was enrolled and whether
its app has registered with the push ring notification ("enrolled 2026-10-08, app registered"). The one with
"app registered" is the phone in use; the others can be deleted under Settings > People.

**On a dashboard, and on a wall tablet.** The `portico-card` Lovelace card (registered by the integration
itself) shows one endpoint's pages (cameras and buttons), and can take over the screen when a door rings. Contract 3: there is no
`profile:`; a card without `endpoint:` shows the viewing user's OWN endpoint (an enrolled phone's login), an administrator's
card names one with `endpoint:` (a screen's slug, or any phone's endpoint id):

```yaml
views:
  - title: Portico
    path: portico
    panel: true
    cards:
      - type: custom:portico-card
        endpoint: kitchen         # a screen's slug (Portico > Screens); omit it on an enrolled phone's own login
        takeover: true            # a ring opens a full-screen overlay (default false: the ring stays inside the card)
        ring_camera: doorbell     # the camera id to show for a door with no `callers` entry (default: the first available one)
        path: /lovelace-kitchen/wall   # optional: the URL path of the view that holds this card; a call that wakes the screen opens it first
```

- **Who may use it**: an enrolled phone's own login (no `endpoint:`), a screen's login (its own `endpoint:`), or an
  administrator naming an `endpoint:`. An administrator without `endpoint:` gets a one-line "Name a screen with
  endpoint:".
- **Pictures** come over the websocket (one still per available camera every 4 s while the page is visible).
  A lock or a gate asks for a **second tap** ("Tap again: ...", which expires after 4 s) before it runs; every
  press goes by action id, exactly as in the app, and shows its result.
- **The ring overlay** is attached to `document.body` (so no ancestor's CSS transform can break its
  `position: fixed`) and shows the door's picture, the profile's open buttons (`portico_open` and `lock_open`),
  Answer (only for a device with audio; a door without it says "answer in the Portico or
  intercom app"), Dismiss, and a chime. It closes itself when the call ends, says "Answered elsewhere" for 3 s
  when somebody else takes it (also when our own Answer loses the race: "Someone else already answered this call",
  no retry offered; when the door station itself refuses with `device_busy` it says "The door station is busy with another call. Try again in a moment." and keeps the ring and Answer so you can press it again), and lets go after 120 s of nobody answering. Dismiss hides it for that call only.
- **What it cannot do:** wake a sleeping kiosk-browser screen or stop its screensaver (use the kiosk
  browser's own settings or a Home Assistant automation); two-way audio with a door that has none; and it has not been seen on a
  real tablet. The chime needs one touch on the page first (a browser autoplay rule); the overlay says so.
- **Contract 3 (M6 E4):** the card speaks contract 3 only: `portico/version` first (any other answer: the line "Portico is
  being updated; this page works again after Home Assistant restarts." and a Reload button), then `portico/doors`,
  `portico/calls/subscribe` and `portico/content/subscribe`, then `portico/content/get`. An administrator's card without
  `endpoint:` gets `unauthorized` from the server and says "Name a screen with endpoint:". - **A declared screen (`endpoint: <slug>`, with optional `fullpage: true`)**: instead of `profile`, the card is one of the
  screens declared in the Screens section of the Devices tab (it needs Portico with M6 K1 or later; the card's Home Assistant user must be the
  screen's user, or an administrator). It shows that screen's pages (tabs when there are several), and on a ring (with
  `takeover: true`) the overlay is built from the screen's `callers` entry for the ringing door: its name, camera stills
  every 1.5 s, and its ring buttons (call buttons once in a call). A door with no entry falls back to the old rule. A second
  ring while the card is in a call (any mode) is refused with one line and the first call continues. A card without
  `endpoint` shows the user's own endpoint the same way.
  `www/tests/test_card_screen.mjs` tests this.
- **The caller's audio.** When the card answers in a browser, the remote WebRTC stream plays through one persistent, hidden
  audio element (never the microphone); if the browser refuses autoplay the call view shows "Tap to hear", and one tap plays
  it (`www/tests/test_card.mjs`, section 9).
- **The kiosk page's back control (`fullpage` only)** is the tablet's standard left-edge back rail (a 64 px round button,
  10 px from the left edge, vertically centred; the look follows the back rail of the maintainer's own dashboards), not an "x". The
  layout reserves its width on the left. It navigates to `back_path` (card config key, default
  `/kitchen-panel/screen`, an estate default left over from the maintainer's installation: set your own) the way HA does (`history.pushState` + `location-changed`, then
  `location.assign`); only a path of one leading `/` is accepted (never `//host`, a scheme, a backslash or a space). With
  `back_path: ""`, an invalid value or a failed navigation it falls back to the kiosk browser's start URL, then `history.back()`, then `/`.
- **The kiosk page's Everyone key and house-call states (`fullpage` only; M7 3d, hidden until house calls are on).** When the
  server's roster for the screen carries a group entry `everyone` (the server omits it while house calls are off and for a
  screen that may not call everyone), the card draws one full-width key under the blocks: "Everyone (2 phones)", or off with
  "No phone is set to receive house calls". One press runs `portico/content/run` with the entry's `action_id` (no confirm
  step); the page then shows an overlay driven by the `house_call` events on `calls/subscribe`: "Calling everyone…" with
  Cancel, "Ringing 2 phones", "<name> answered", "In a call with <name>" with Hang up, and the outcomes (declined, no
  answer, busy, could not connect) in words. A phone calling the screen shows "<name> is calling" with Answer and Decline.
  A door ring always takes the screen from a house call. With the key absent nothing is drawn and the page is byte-identical
  to the page before 3d. No audio yet (3b). `www/tests/test_card_house.mjs`; look: `test_card_visual.html?wide-everyone`.
- `www/tests/test_card_content.mjs` (Node, jsdom) tests all of this; `www/tests/test_card_visual.html` renders
  it with a fake `hass` for a look (`firefox --headless --screenshot`, `?ring`, `?ring-audio`).
