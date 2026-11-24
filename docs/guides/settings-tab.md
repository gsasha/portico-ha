# The Settings tab

The panel's Settings tab: door status, door settings and push settings, with their websocket commands.

## The Settings tab (UI split P4, P5, P6)

The Portico panel's **Settings** tab (administrators only) has three parts. It is its own module, `www/portico-settings.js`
(served and content-hashed like the Phones tab; a failed import leaves no Settings tab and nothing else changes), it writes
only when an administrator presses **Apply**, and the Home Assistant dialogs stay as the recovery path.

* **Door status** (P4, read only): a card per door entry with its name, driver, whether it is connected, the hub's process and
  wheel version, the call and Talk states and whether the door can place a call. No token, password or address is shown.
* **Door settings** (P5): since the gate observer door was retired (DEV-518 C5) a door has nothing here that the panel can change;
  the tab keeps the command for the wire contract, it accepts only the deprecated `away_media` (ignored) and refuses the former
  observer settings as `not_for_this_driver`. A `gate_sip_server` door's switches (Talk to the gate, the ring-answered helper, the
  door-open action, house calls) are in Home Assistant's own **Reconfigure** dialog.
* **Push ring notification** (P6): ring on or off, the ring window (10 to 120 s), whether new phones get the push ring notification, and the **test
  phones** list (while it is not empty a real ring goes only to the listed phones that have their push ring notification on). These are the
  options of the settings entry that its Configure menu edits, applied at once. No house calls here: there is no global House calls switch any more (D-20261122-aafb). The same card has **Live video away from home** (DEV-713 S1b-1): the address of the camera server's RTSP port
  (empty = off) and the cameras that have a `<camera>_sub` stream. Its text says in plain words that anyone with a Portico
  phone on the tailnet who may reach that port can pull ANY stream on it, not only the cameras shown to them; the change goes
  through the confirmation page.

Five websocket commands, all `admin` in `auth.COMMANDS`, in `door_commands.py` (door) and `ui_settings.py` (push ring notification):

| Command | Does |
| :--- | :--- |
| `portico/door_status {}` | `{hub, doors, as_of}`: the read described above. Per door: `entry_id`, `name`, `driver`, `loaded`, `entry_state`, `connected`, `transport_available`, `driver_available`, `call_state`, `outbound_state`, `can_dial`, `away_media` (deprecated: always `true`); `settings`, `gate_sip` and `listen_state` are always `null` (the observer's, kept so an older panel keeps working). |
| `portico/door_settings/get {}` | `{doors, helpers, effects}`: per door `entry_id`, `name`, `driver`, `loaded`, `revision`, `away_media` (deprecated: always `true`), `listen` (always `null`). `helpers` lists the `input_boolean`s, `effects` says per key `live` or `reload`. |
| `portico/door_settings/set {entry_id, revision, patch, confirm?, validate_only?}` | `patch` is a non-empty object; `away_media` (deprecated) is accepted and ignored, a stale `revision` is `stale_revision`, an unknown key or a value of the wrong kind is `bad_patch`, and any former observer key (`listen_enabled`, `audio_port`, `claims_enabled`, `ring_answered_entity`, `dial_enabled`, `house_calls`) is `not_for_this_driver`. |
| `portico/ui_settings/get {}` | The push ring notification's settings: `ring_enabled`, `ring_window` (and its limits), `new_phones_enabled`, `test_phones`, `phones` (`reg_id`, `name`, `away_ring`), whether the settings entry exists and whether the push ring notification is running; and, only while set, `live_rtsp_base` and `live_sub_streams` (live video away from home, see "Live video of a camera tile"). |
| `portico/ui_settings/set {patch, confirm?, validate_only?}` | `patch` is a non-empty object of `ring_enabled`, `ring_window`, `new_phones_enabled`, `test_phones` (registered phones only: `unknown_phone`), `live_rtsp_base` (`rtsp://host[:port]` only, `""` clears it) and `live_sub_streams` (camera names, `[]` clears it); a value that does not validate (credentials in the address, a path, a name with a slash) is `bad_patch` and nothing is written. **Every key but `ring_window` needs `confirm: true`, checked by the server** (the two live keys too, turning them off included). Applied at once through the settings entry's update listener; a change to either live key also announces `content_changed` to every subscriber. Replies `{result, changes, settings}`. |

Honest limits: the dialog does not check revisions, so a dialog form left open can overwrite a panel change (a dialog write
changes the hash, so the next panel write sees it). The dialogs are not gated by the idle rule, as they never were.
