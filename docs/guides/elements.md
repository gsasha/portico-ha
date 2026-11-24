# Elements: the panel's shared building blocks

The Elements tab of the panel: renderers, the elements store, its websocket commands and the camera-size display in the form (DEV-760).

Related: [content, pages and actions](content-pages-and-actions.md).

## Elements

**Elements renderers (DEV-760 E3).** `www/portico-elements-render.js` is a pure ES module (served and content-hashed like the
tab modules; its URL travels in the panel config as `elements_render_module`) that draws the four element kinds (camera,
button, status, chip) as `{el, update(states), destroy()}` with DOM builders only. A button is drawn disabled and inert
("Preview: does not run"); nothing in it calls HA. Nothing imports it yet: the Elements tab (E4) will. The card and the
kiosk page do not use it in this step, so the guarantee that a preview matches the screens is a test, not shared code:
`www/tests/test_elements_render.mjs` pins its palette, default icon per action and ask-first set equal to the card's, the
panel's and the two `contract/` files. A new kind is one row in its `RENDERERS` table plus the server's `KIND_SPECS` row.

**Elements tab (DEV-760 E4, read-only).** `www/portico-elements.js` is the Elements tab, imported lazily by the panel like Phones
and Settings (URL in the panel config as `elements_module`; administrators only; a failed import of it or of the renderer
leaves no tab and breaks nothing). It lists `portico/elements/list` grouped by kind with a live preview of each element drawn
by the shared renderer, an empty-state line and error lines; it has no add, edit or remove in E4 and writes nothing (E5 adds them, below). Previews
follow the states through the panel's `set hass` and, as a fallback while the tab is open, a `state_changed` event
subscription (whether HA calls `set hass` on a custom panel at each change is unverified). Needs an HA restart to load the
new config key; the `www/` files themselves are read on page load.

**Elements store and commands (DEV-760 E1, E2).** `elements.py` keeps the elements in their own file, `.storage/portico.elements`
(`{schema_version: 1, elements: [...]}`, list order = display order), separate from `portico.content`, which it never reads or
writes. An element is `{id, kind, name, icon?, entity_id?, options}`; the server mints the `id` (`el-` plus 8 hex) and it
never changes, the name is unique case-insensitively and renamable. A kind (camera, button, status, chip) is a row in
`KIND_SPECS`. `rev` (hash of the stored list) is the `base_rev` of every write; a file with a newer `schema_version` is read
but never rewritten (`newer_schema`); `MIGRATIONS` is the empty migration hook. `elements_commands.py` registers three
`admin` commands: `portico/elements/list` (elements, `rev`, `used_by`, the kind registry the form is built from),
`portico/elements/set {element, base_rev, validate_only?}` (create without `id`, replace with it; `kind` cannot change) and
`portico/elements/delete {element_id, base_rev, confirm?}`. Nothing here calls a service or writes to HA.
`ElementStore.used_by(element_id)` is the seam for the "screens use this element" warning: it returns `[]` until screens
reference elements, and `delete` already refuses with `in_use` (plus the screens) unless `confirm: true`. The stream-only
camera is allowed by the flag `elements.CAMERA_ALLOWS_STREAM` (design question A, unanswered): `False` makes it entity-only.

**Elements tab (DEV-760 E5, add / edit / remove).** The same tab now adds, edits, renames and removes elements. The draft is
drawn by the shared renderer before saving and checked by the server with `validate_only` (nothing written); the entity and
icon fields use HA's `ha-entity-picker` / `ha-icon-picker` when the browser has them, else the panel's own list. Errors show
a sentence per server code; `conflict` and `unknown_element` offer a reload. A button stays an inert preview. Browser-only
change (the `www/` files), no restart. The real pickers and previews are not exercised by the offline suite.

**Camera sizes in the Elements form (D-20261109-b0a3).** For a camera element the entity field is always the panel's own list
(HA's `ha-entity-picker` cannot show a size) and each row reads `Name (camera.id) \u2014 2560x1440`: `…` while the size is being
read, `unknown` when the server has none (an unavailable camera, an older server, a failed read). The size comes from the
existing admin command `portico/cameras/info` (camera_info.py: the header of the still the camera returns now, `source`
`still`, or the entity's own `width`/`height`), at most 20 ids per call, cached 10 minutes in the browser (a failure 1 minute) on top of
the server's own cache. It is the size of the snapshot, so a live stream may differ, UNLESS the row has `stream: {width, height}` (DEV-766): a camera whose entity offers a plain `rtsp://` address (Frigate's restream, a Generic Camera on it) gets the size of its LIVE stream, read from the stream's own RTSP description (camera_stream_size.py: one DESCRIBE, the H.264 SPS, no ffmpeg), and the picker, the caption and the Pages spec chip show that size and say `live stream`; `width`/`height` stay the snapshot's. Without it nothing changes. Rows are sorted by camera family (the entity id
without a trailing `_main`, `_sub`, `_high_resolution_channel`...), largest picture first inside a family. Browser-only change, no restart.
