# Content: pages, blocks and actions

What the app and a dashboard show: the per-phone content profile (pages, blocks, cameras, action kinds), the rule that the profile is the grant, the install-wide home name, and the editor.

Related: [call buttons and Talk](call-buttons-and-talk.md), [live video](live-video.md), [dashboard card and panel](dashboard-card-and-panel.md), [Elements](elements.md).

## Content: what the app and a dashboard show

Each enrolled phone has a **content profile**: an ordered list of cameras and actions, kept in
`.storage/portico.content` (not in a config entry, so editing it never reloads the integration).

| action `kind` | needs | what the plugin does |
|---|---|---|
| `portico_open` | a Portico `device_id` | opens that device's relay |
| `lock_open` | a `lock` entity | `lock.open` |
| `button_press` | a `button` entity | `button.press` |
| `script_run` | a `script` entity | `script.turn_on` |
| `scene_activate` | a `scene` entity | `scene.turn_on` |
| `call` | a call `target` (`{type: dashboard\|door\|phone, id}`) | places a call through the call-endpoint registry (see [Call buttons and Talk](call-buttons-and-talk.md)); nothing on a default install |

Anything else is refused when a profile is saved, and again when an action runs. Lights, switches
and covers are not in this first phase.

**Blocks (store schema 4).** A page is `{id, label, blocks}` and a block `{id, title, door?, cameras, actions}`; a
camera may carry `size` (`auto`, `big`, `small`; the layout size) and `live` (a boolean, see [Live video](live-video.md)), a button `color` (a palette name), `show_label` and, besides the kinds
above, one of `lock_lock`, `lock_unlock`, `cover_open`, `cover_close` (a `lock` or `cover` entity; the service runs
with the caller's HA user) or `talk` (acts on the block's `door`; see [Talk to the door](call-buttons-and-talk.md): the editor can create it; the phone greys it, and nothing dials, until a door reports `CanDial`). A
`portico_open` may omit `device_id` in a block that has a `door`. Limits: 8 blocks a page, 12 cameras and 12 buttons a
block, 24 and 24 a page; ids are unique per page across blocks. A v3 file is read as ONE block per page (id = the page
id, title = the page label, nothing dropped) and rewritten as schema 4 only at the first save, which first leaves
`.storage/portico.content.v3`, which is the way back to a schema-3 build. The file on disk holds `blocks` and nothing
else on a page: the flat `cameras`/`actions` mirror that files written between the first blocks release and the one that
dropped the flat form carried is ignored when read and gone at the next save.

**An empty camera name.** A camera's `label` may be empty (or blank, which is stored as `""`): the picture identifies
itself, so the card and the editor's preview draw no name line at all for it (the Android app's own drawing is its own change). The key stays on the
wire with the value `""` (never dropped, never replaced by the entity's name); the picture keeps a neutral accessible
name ("Camera"). A page's, a button's and a call button's label are still required. In the editor the row of an unnamed
camera shows the entity's own name in muted text; that is display only and is never written into the stored label.

**Blocks on the wire (still contract 3, additive).** `portico/content/get` pages are `{id, label, blocks}` and nothing
else: the flat `cameras`/`actions` beside `blocks` that earlier releases served for app 0.5.2 and older and for the
card on disk are gone, from `content/get`, `content/document` and `content/preview` alike. `blocks[]` is `{id, title,
door?, door_available?, cameras, actions}`: every camera carries its effective
`size` (`big`/`small`: an explicit size as stored, `auto` is `big` for the first camera of a block and `small` for the
rest), every button its kind (the new kinds and `talk` included), `device_id` (a bare `portico_open` gets its block's
`door`), `color` and `show_label`; `talk` resolves `{available, reason}` from the door (see [Talk to the door](call-buttons-and-talk.md): `available: false, reason: "talk_off"` for as
long as no door reports `CanDial`, which is every door until the owner turns the dial on).
`door_available` is true when the block's door is known and up; it is live state and, like `available`, not part of
`rev`. A `content/save` or `content/preview` document must say `schema_version: 4` (3 is refused: reload the page) and
carry its pages as `blocks`: a page with `cameras` or `actions` is refused ("update the Portico panel"), because it
comes from an editor older than the blocks panel and its edit would otherwise be dropped without a word. **Who reads
what.** The Portico app from 0.5.3-alpha, the panel and the card/kiosk read `blocks`; the app wraps a page with no
`blocks` key as one block and the card keeps its flat fallback for an OLD server only. App 0.5.2-alpha and older read only
the flat lists and show an empty Home against this server until they are updated.

**The profile is the grant.** A phone runs an action with `portico/content/run {action_id}`. The
plugin finds that id in the **calling phone's own** profile and makes the call itself, as that phone's
user. The phone never sends a service name or an entity id, and cannot run another phone's action.
So the entity does not have to be in the phone user's HA permissions; what you put in the profile is
what that phone may do. Opening a lock or a gate defaults to `confirm: true` (the app requires confirmation).
Each action can run once every 2 seconds per phone, and every run, including a refusal, is written to
the logbook as `Portico: <phone>`.

`portico/content/get` returns the endpoint's document (contract 3: `endpoint`, `kind`, `rev`, `home_name` and `show_fps` (true only) when
set, `pages` (with `blocks`, above) with each entity's availability (`available: false` for an unavailable or missing
one), `callers`, `roster`; no flat `phone`/`cameras`/`actions` at the top level) and a `rev` that changes when the
content changes (a size or colour edit does; a camera blinking or the gate going down does not).
`content/get|run|snapshot|live|subscribe` take one optional key, `endpoint`: an enrolled phone gets its own, a declared
screen its own (it names one when it serves several), and an administrator names any phone or screen by its endpoint
id. `profile` (a phone's name) is gone.

### The home name (install-wide)

The install's **home name** is one name for the whole home, such as `Our House`. The Configure menu no longer
sets it (the Configure content flow was removed in blocks E7, DEV-658); the Portico panel sets it (Pages tab, the household template: a field of the document, saved with it, no command of its own),
a name already stored stays and is served as before. A name is
up to 40 characters after trimming, with no control characters or line breaks; empty means not set.
It is a setting of the install, not of a phone or of one device entry, so it is kept in the content store
(`.storage/portico.content`, key `home_name`, absent when unset) and every enrolled phone gets the same
one. There is no default in the code: unset, the app shows "Portico".

`portico/content/get` carries it as an optional top-level string in the result, next to `endpoint`, `rev`
and `pages`: `{"endpoint": "...", "rev": "...", "pages": [...], "home_name": "Our House"}`. The field is
**omitted** when no name is set (clients ignore fields they do not know). While a name is set it is part of
`rev`, so a client that compares revs refetches when only the name changed. A change to the name sends
every subscriber of `portico/content/subscribe` a `{"type": "content_changed", "endpoint": ..., "rev": ...}` event for
its own endpoint (`endpoint: null, rev: null` for an administrator who named none). Saving the same name again
sends nothing.

## Editing it in the panel

**Editing it:** open **Portico** in the Home Assistant sidebar (administrators). The Pages tab edits each phone's
pages, and in each page its **blocks** (add, rename, reorder, delete; a block may name a door; at most 8 per page, 12
cameras and 12 buttons per block). A camera is Automatic, Big or Small; a button has what it does (open a door, open,
lock or unlock a lock, open or close a gate or garage cover, press a button, run a script or scene; Talk is listed and
stays off), its name, an optional icon, one of eight palette colours, whether the name is shown (hiding it needs an
icon) and whether it requires confirmation. A block over the limit (a page that came from schema 3 can hold 24) shows a
"Split this block" button. The panel sends schema 4 and never the flat `cameras`/`actions` (the server refuses them). The preview draws the
draft the way the phone will. The old Configure > "Choose what a phone shows" flow is gone, because it wrote a flat
profile over the home page and would have collapsed a multi-block page.
The icon is a choice over a fixed list of about a hundred Material Design Icons (`ICON_NAMES` in
`content.py`, also `contract/icons_v1.json`: the list the phone app bundles and draws before the button's text).
"No icon of my own" stores nothing and the app uses a default for the button's kind. A profile that already
holds any other icon string is still accepted and kept; the app draws nothing for a name it does not bundle.
The same JSON is copied into portico-app (`core/src/test/resources/icons_v1.json`); a test on each side
fails if the two lists differ, so a new icon is added to the JSON in both repos, here to `ICON_NAMES`, and
there to `HomeIcons.CURATED` plus a drawable (its `tools/gen_mdi_drawables.py`). The panel saves with Save and the app is told. To run
anything beyond the table above, make a script in Home Assistant and add a "Run a script" button.
