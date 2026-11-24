# Updating the integration

What to do after an update so the browser runs the new card and panel.

## After an update

Restart Home Assistant, then reload the browser page. The card, the panel and the Phones and Settings modules are each
registered under a URL carrying the hash of their own file (`?v=<12 hex>`), fixed at the restart, so a reload fetches
whatever changed. Every Portico script is also sent with `Cache-Control: no-cache`: a browser keeps it but asks Home
Assistant before each use (an unchanged file answers `304`), so a copy cached under an old or unrelated URL cannot be
reused unasked. Older builds sent the scripts with no `Cache-Control` at all, which lets a browser reuse a copy for
days without asking, and one panel ran the previous editor until a hard reload. If a screen still shows
the old code after a restart and a reload: hard-reload the page (Ctrl+Shift+R); in the Companion app, Settings >
Companion app > Troubleshooting > Reset frontend cache; Home Assistant's own service worker (a kiosk browser's own cache
clear does not reach it) is cleared by the browser's "clear site data" for the Home Assistant address.
