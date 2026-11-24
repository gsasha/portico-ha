# Setting up a wall tablet as a ring screen

How to make a tablet or any screen that stays logged in to Home Assistant show the door's pictures and buttons, and take over the screen when the door rings. Called a "screen" in Portico; a phone is a different kind of endpoint.

Related: [the dashboard card, wall tablet and panel](dashboard-card-and-panel.md), [content, pages and actions](content-pages-and-actions.md), [Elements](elements.md), [call buttons and Talk](call-buttons-and-talk.md), [AI-guided setup](../getting-started/ai-setup-prompt.md).

## What you get, and what is unverified

- **An idle page** with the camera tiles (a still picture every few seconds) and the buttons you place on it. **Seen on a real wall tablet** by the maintainer in 2026-10 (idle page, back rail).
- **A ring takeover:** when a door rings, a full-screen overlay shows the caller's camera, your ring buttons, Answer (only for a door that carries audio), Dismiss and a chime. It closes when the call ends, says "Answered elsewhere" for 3 seconds when another device takes it, and lets go after 120 seconds. As of the maintainer's last notes (2026-10-24) the takeover was **tested offline with mock data and not yet seen with a real ring on a real tablet; the browser Answer path (WebRTC audio in a tablet browser) was never exercised on a live call: unverified.** Treat your first real ring as a test.
- **What it cannot do:** wake a sleeping screen or stop a screensaver (use your kiosk browser's own settings or a Home Assistant automation that turns the screen on); two-way audio with a door that has none; play the chime before one touch on the page (a browser autoplay rule; the overlay says so).

## Before you start

- Portico installed, a door added and ringing ([first run](../getting-started/first-run.md)).
- A tablet (or any device) with a current browser, on the same network or the tailnet, that can open your Home Assistant address. A kiosk browser such as Fully Kiosk is common; any browser kept open works.
- A **Home Assistant user for the screen.** Make one under Settings > People > Users, **not an administrator** (the panel lists only non-administrators for a screen). Give it a long password; you will log in once on the tablet and the session stays.
- Elements (cameras, buttons) created for the pages you want, see [Elements](elements.md).
- Decide the screen's address, a short slug such as `kitchen` or `hall`. Letters, digits and dashes.

## Step 1: declare the screen

Portico panel (sidebar, administrators) > **Screens** tab (in older builds the Screens section of the Devices tab) > add a screen:

| Field | Value |
| :--- | :--- |
| Name | what you call it, for example `Hall tablet` |
| Address (slug) | `hall` |
| Home Assistant user it is logged in as | the user you made |
| Room | optional; a label |

Then, on the screen's pages, place the cameras and buttons ([Pages tab](content-pages-and-actions.md)). In the **Ring & call** tab set what a ring from each door shows: which camera and which buttons (open the gate, open the door). Locks and gates ask for a second tap before they run.

Every action runs as the screen's own Home Assistant user, and only what is on that screen's profile can be run.

## Step 2: point the tablet at the kiosk address

The Screens tab shows the screen's address, built from the address you opened Portico at:

```
https://ha.example.net/portico/kiosk/hall
```

1. On the tablet, open Home Assistant at the **same address** (the same scheme, host and port; a different origin logs the screen out) and log in as the screen's user.
2. Open the kiosk address. It shows the idle page for that screen and nothing else (no sidebar, no header). If it says the user is not the declared one, you are logged in as someone else. If it says the card is not loaded, reload; if that persists the card file is not being served.
3. Set it as the kiosk browser's start URL (Fully Kiosk: Settings > Web Content Settings > Start URL), so a reload returns to it.
4. Keep the screen awake and the browser permitted to autoplay audio and use the microphone for this site if you want to answer from the tablet (Fully Kiosk: Device Management, Web Content Settings). **Unverified** per tablet model.

The kiosk page has a round back control on its left edge that returns to a path you choose (`back_path`, default `/kitchen-panel/screen`, a leftover estate default; set your own in the card config if you use the dashboard card below).

### Alternative: the card on a dashboard you already use

If the tablet shows a Lovelace dashboard, add the Portico card to a view instead:

```yaml
type: custom:portico-card
endpoint: hall         # the screen's slug
takeover: true         # a ring opens a full-screen overlay
fullpage: true         # optional: behave like the kiosk page
path: /lovelace/wall   # optional: the view that holds this card; a call opens it first
```

The card's Home Assistant user must be the screen's user (or an administrator). See [the dashboard card](dashboard-card-and-panel.md) for every option.

## Step 3: test

1. Open the page; check that cameras show stills and buttons are present.
2. Press a harmless button (not a lock) and confirm its result shows.
3. With someone you trust at the door, ring it. The overlay should appear within a second or two with the camera, a chime (after one touch), and the buttons. Dismiss it.
4. Ring again and answer from the tablet. Check audio both ways.
5. Ring and answer on a phone instead; the tablet should say "Answered elsewhere".
6. Reboot the tablet, wait, ring again. If the page does not come back, check the start URL and that the session is still logged in.

## Troubleshooting

| Symptom | Likely cause |
| :--- | :--- |
| Logged out again after a reload | the origin differs from where it was logged in (scheme, host or port) |
| "Name a screen with endpoint:" | an administrator's card without an `endpoint` |
| Overlay never appears | the page was not open (kiosk browser killed it), the screen user lost its profile, or the door is not on the Ring & call rules; check the Portico connection log ([connection log](connection-log.md)) |
| Overlay appears but no sound | one touch has not happened since load (autoplay rule), or the tablet's media volume is off |
| Answer does nothing, or says the door cannot answer | the door has no audio route (a ring-only door), |
| Pictures old or blank | the camera entity is unavailable; the tile is hidden while it is |
