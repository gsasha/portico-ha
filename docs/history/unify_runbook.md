# The unification window (DEV-686, U1): runbook and rollback

> **History page.** The dated record of the maintainer's own one-off migration of the former `portico_intercom` entry into `portico` (2026-10-24). Entry ids and host paths are shown as placeholders where they were specific to that installation. A fresh install never needs it; it is kept for the rollback reasoning and as an example of a supervised offline migration.

What this does: the Home Assistant integration `portico_intercom` (the door side) becomes part of `portico`. The live door
entry (domain `portico_intercom`, driver `gate_sip_observer`, entry id `<entry_id>`) keeps its entry id, its
entities (`sensor.call_state`, `sensor.listen_leg`), their history and its stored peer token, by an **offline rewrite of two
storage files with Home Assistant stopped** (`scripts/migrate_unify.py`). There is no automatic migration inside Home
Assistant (Option B of `portico_unify_design.md`), and **no one-line undo**: the way back is the dated copy of the files
(section 5), about ten minutes.

Portico is not in production yet; a momentary break of the Portico side is accepted. gate-sip and the old app are never
touched. Nothing in this document restarts anything by itself: the maintainer names the window, the gate is quiet, the maintainer is at the host.

**Status: the window was run on 2026-10-24 and it worked** (section 8 is the dated record). Every command below was checked
against the code in this repository, against Home Assistant's source, or on the box in that window (marked **VERIFIED
2026-10-24**). What is still marked **UNVERIFIED** was not exercised then. The rollback (section 5) was **not needed** and
stays as the reference for any future window of the same kind.

## 0. Read the box first (nothing changes)

These answer what the runbook cannot know. Do them any time, Home Assistant running.

1. **Where is the Home Assistant config directory from the shell you will use?** In the SSH add-on it is `/homeassistant`
   (**VERIFIED 2026-10-24**; `/config` is the same directory seen from Home Assistant core's own container, and older add-on
   versions used it in the shell too).
   ```
   ls /homeassistant/.storage/core.config_entries
   ```
   This runbook writes `/homeassistant` as `CONFIG`.
2. **How does the code reach the box: a symlink to a git checkout, or a copy?** **VERIFIED 2026-10-24:** symlinks.
   `custom_components/portico` and (before the window) `custom_components/portico_intercom` were symlinks to
   `/config/portico-ha-src/custom_components/...`. The checkout is `/homeassistant/portico-ha-src` in the SSH add-on (the
   same directory as `/config/portico-ha-src` inside Home Assistant core), so `REPO=/homeassistant/portico-ha-src` and the
   scripts are under `$REPO/scripts/`.
   ```
   ls -la /homeassistant/custom_components/
   ```
   A plain directory instead would be a copy (the a shell command copies into it); section 2 step 6 handles both.
3. **Is there Python, and where is the script going to run?** The script is standard library only.
   ```
   python3 --version          # VERIFIED 2026-10-24: Python 3.14.8 in the SSH add-on
   ```
   Had there been none: `apk add python3` in the add-on (**UNVERIFIED**), or run it in a throwaway container with the config
   mounted (needs the host Docker; **UNVERIFIED** on HA OS):
   `docker run --rm -v /mnt/data/supervisor/homeassistant:/config -v "$REPO/scripts":/s:ro python:3-alpine python /s/migrate_unify.py --config /config ...`
   (`/mnt/data/supervisor/homeassistant` is the host path of the config directory on HA OS; **UNVERIFIED**.)
4. **Where is the script?** `$REPO/scripts/migrate_unify.py` (**VERIFIED 2026-10-24**: the box has the checkout, so
   the maintainer's sync job brings the script with the merge). With a copy-only box, copy that one file to the box first (it needs
   nothing else).

### Rehearse on a copy (any time, zero risk, Home Assistant running)

```
CONFIG=/homeassistant
R=/tmp/unify-rehearsal; mkdir -p $R/.storage
cp -p $CONFIG/.storage/core.config_entries $CONFIG/.storage/core.entity_registry $R/.storage/
cp -p $CONFIG/.storage/portico.gate_sip_peer.* $R/.storage/
python3 $REPO/scripts/migrate_unify.py --config $R --dry-run
```
It prints exactly what it would change (the entry, the two entities) or `nothing to do`. To see the result itself, make the
backup folder of the copy and run it for real **on the copy**:
```
mkdir $R/bk && cp -p $R/.storage/core.config_entries $R/.storage/core.entity_registry $R/.storage/portico.gate_sip_peer.* $R/bk/
mkdir $R/bk/portico_intercom
python3 $REPO/scripts/migrate_unify.py --config $R --backup-dir $R/bk --quiet-seconds 0
diff $R/bk/core.config_entries $R/.storage/core.config_entries
diff $R/bk/core.entity_registry $R/.storage/core.entity_registry
```
Expect: the entry's `"domain"` line and one added `"entry_type": "door"` line, and one `"platform"` line per door entity.
(The copy has no `.ha_run.lock` and no hub socket, so the "is it running" checks have nothing to find; on the real
directory they apply.) On this box the rehearsal printed the warnings listed in section 2 step 4, which are normal.

## 1. Before the window

* The gate is quiet (nobody expected); the maintainer is at the host with an SSH shell. An assistant reads the states afterwards, the maintainer presses
  the bell for the last check.
* **Do not merge U1a/U1b before the window starts.** On the maintainer's installation a merge to `main` lands on the box's disk by
  the maintainer's sync job and the old `custom_components/portico_intercom/` disappears from under the running Home Assistant (what is
  already imported keeps running; lazy imports such as the Reconfigure form would fail). Hold every other `portico-ha` merge
  until this is done. Any Home Assistant restart between the merge and the migration **is** U1 at a time nobody chose: if
  that happens by mistake, section 6.
* Record the "before" state, to compare after:
  ```
  curl -s http://<gate-sip host>:8091/health ; echo          # gate-sip, from the entry's gate_sip_url
  ```
  and in Home Assistant (Developer tools > States, or the REST call in section 4): `sensor.call_state`, `sensor.listen_leg`.
* Optional extra safety, not the rollback: a Supervisor backup, `ha backups new --name pre-unify-$(date +%F)` (**UNVERIFIED**
  options; a full-backup restore would roll back everything since it was taken, so it is not the plan).

## 2. The window, in order

Variables used below (set them once in the shell):
```
CONFIG=/homeassistant                      # section 0, step 1
REPO=/homeassistant/portico-ha-src         # the checkout on this box (section 0, step 2)
BK=$CONFIG/unify-backup-$(date +%Y-%m-%d_%H%M)
```

1. **Merge** `[Unify U1a]` and then `[Unify U1b]` (the maintainer merges; nobody else). The sync job puts the code on the box. Nothing
   is restarted. Do not reload any Portico entry from the UI from here on.
2. **Stop Home Assistant.**
   ```
   ha core stop
   ```
   `ha core stop` and `ha core start` work and print `Processing... Done.` (**VERIFIED 2026-10-24**). **Do not use `ha core
   info` to check the core is down: it shows no running/stopped field** (**VERIFIED 2026-10-24**). The check is the script's own
   refusal checks (steps 4 and 5) plus
   `curl --max-time 3 -s -o /dev/null -w '%{http_code}\n' http://homeassistant:8123/`, which prints `000` when it is down
   (the host name `homeassistant` is **UNVERIFIED**; `localhost:8123` is the alternative).
   **Wait a minute before the dry run:** Home Assistant writes `core.entity_registry` while it shuts down, so the script's
   60-second guard (step 5) fires right after `ha core stop` (**VERIFIED 2026-10-24**). Wait, then dry-run again.
3. **Take the dated copy** of the four things the migration touches or depends on (the script verifies these byte for byte before
   it writes anything):
   ```
   mkdir "$BK" "$BK/portico_intercom"
   cp -p $CONFIG/.storage/core.config_entries $CONFIG/.storage/core.entity_registry "$BK"/
   cp -p $CONFIG/.storage/portico.gate_sip_peer.* "$BK"/
   find $CONFIG/.storage/portico_intercom -type f -exec cp -p {} "$BK/portico_intercom/" \;
   ls -l "$BK" "$BK/portico_intercom"
   ```
   (The folder `portico_intercom/` holds the hub's socket while Home Assistant runs; a socket cannot be copied and is recreated
   at start, so only regular files are copied. After a clean stop it usually holds none.)
4. **Dry run** (writes nothing):
   ```
   python3 $REPO/scripts/migrate_unify.py --config $CONFIG --dry-run
   ```
   Expected: one line `entry <entry_id> 'Portico (gate_sip_observer)' ... domain portico_intercom -> portico,
   data.entry_type = door`, one line `entity registry: platform portico_intercom -> portico on 2 row(s): sensor.call_state,
   sensor.listen_leg`, one line `peer-token Store portico.gate_sip_peer.<entry_id>: present`, and `dry run:
   nothing was written`. **Stop and look if anything else
   appears** (another entry, another row). Three warnings are **NORMAL on this box** (**VERIFIED 2026-10-24**): two lines
   saying a file is **not formatted the way this script writes it**, and one saying a **registry row is not tied to a migrated
   door ... deleted_entities: sensor.call_state** (an old tombstone in `deleted_entities`). Any other warning: stop and look.
5. **The real run.**
   ```
   python3 $REPO/scripts/migrate_unify.py --config $CONFIG --backup-dir "$BK"
   ```
   Expected last line: `done: 1 door entry/entries and 2 registry row(s) migrated.` It **refuses** (exit status 2, nothing
   changed) when Home Assistant looks running (the single-instance lock `.ha_run.lock` is held, the hub socket answers, the files
   were modified in the last 60 s, or a `.HA_RESTORE` exists), when the backup folder is missing or differs, or when a `portico`
   entry already has the door's unique id. Exit status 1 is a malformed file or a failed write (the originals are put back).
   Running it a second time prints `nothing to do` and changes nothing.
6. **Make sure the old code is not on the box.**
   ```
   ls -la $CONFIG/custom_components/
   ```
   * `portico_intercom` is **absent**: nothing to do.
   * It is a **symlink** (dangling now, because the checkout no longer has the folder; **this is what the box had on
     2026-10-24**): harmless, but move it away so the next reader is not confused:
     `mv $CONFIG/custom_components/portico_intercom "$BK/old-symlink-portico_intercom"`.
   * It is a **directory** (a stale copy): it would load beside the new code and its hub would fight for
     `.storage/portico_intercom/hub.sock`. Move it away: `mv $CONFIG/custom_components/portico_intercom "$BK/old-code-portico_intercom"`
     (this also keeps the old code for the rollback).
   Also check the new code is there: `ls $CONFIG/custom_components/portico/door/hub.py` and that
   `$CONFIG/custom_components/portico/manifest.json` has no `portico_transport` in `requirements`.
7. **Start Home Assistant.**
   ```
   ha core start
   ha core logs 2>&1 | grep -i portico | tail -40         # the log namespace is custom_components.portico.door
                                                          # (UNVERIFIED: `ha core logs` flags)
   ```

The hands-on time of the window was about ten minutes (**VERIFIED 2026-10-24**).

## 3. The checks (five minutes; same as stage 1 of the activation runbook)

An assistant reads the counters and states; the maintainer presses the bell. With a long-lived token in `$HA_TOKEN` (never paste it anywhere):

| # | Check | How | Expected |
| :--- | :--- | :--- | :--- |
| 1 | The door entry is loaded in `portico` | `curl -s -H "Authorization: Bearer $HA_TOKEN" http://homeassistant:8123/api/config/config_entries/entry` (an admin token; **UNVERIFIED** host name, or use `localhost:8123`) | an entry with `"domain":"portico"`, `"entry_id":"<entry_id>"`, `"state":"loaded"`; no entry of domain `portico_intercom` |
| 2 | The same two entities, same ids | `curl -s -H "Authorization: Bearer $HA_TOKEN" http://homeassistant:8123/api/states/sensor.call_state` and `.../sensor.listen_leg` | `call_state` is `idle`, `listen_leg` is `idle`/`listening`; `.../api/states/sensor.call_state_2` is `404` |
| 3 | History continues | the History panel for `sensor.call_state` | the line continues from before the restart (the id is unchanged) |
| 4 | No repair card for the old integration | Settings > Repairs | nothing named `portico_intercom`; **never** click a "remove entries" fix |
| 5 | The hub is attached, with the peer token | Settings > Devices & services > Portico > the door entry > Download diagnostics | `hub.process: running`, `doors_attached: 1`, `wheel_version: 0.6.2`; `runtime.listen_state` is not null once the leg connects; no secret in the file |
| 6 | The wheel was NOT reinstalled | the log: no line `portico: installing the transport wheel` | absent (the installed wheel is already the pin: a normal boot makes no network call) |
| 7 | gate-sip is unaffected | `curl -s http://<gate-sip host>:8091/health` | the same as before the window |
| 8 | A real ring | the maintainer presses the bell | `sensor.call_state` goes `ringing`, `listen_leg` `listening` then the leg leaves, the **old app rings and answers as before**, the Portico app and the card show the call |

Anything in 1, 2, 5 or 8 wrong: section 5 (rollback) is the answer, not a repair on the box.

## 4. The names that did not change (so nothing downstream moves)

The bus event `portico_intercom_call`, `sensor.call_state` / `sensor.listen_leg`, the door provider id `portico_intercom` (and so
every `portico_intercom:<device id>` endpoint id, content rule, caller and push `door_ref`), the hub socket
`.storage/portico_intercom/hub.sock` and the peer-token Store `portico.gate_sip_peer.<entry id>`. Automations and dashboards that
use them keep working untouched.

## 5. Rollback (about ten minutes; the gate is unobserved by Portico meanwhile, gate-sip and the old app are never touched)

After U1 there is no "revert the PR and restart": the code revert alone leaves a `portico` door entry that the old code refuses.
The rollback restores the dated copy **and** the old code, and only then starts Home Assistant.

1. **Stop Home Assistant:** `ha core stop`. (Do not start it again until step 5.)
2. **Restore the storage files** from the dated copy (`BK` as in section 2):
   ```
   cp -p "$BK"/core.config_entries "$BK"/core.entity_registry $CONFIG/.storage/
   cp -p "$BK"/portico.gate_sip_peer.* $CONFIG/.storage/
   ```
   (The peer-token Store and the `portico_intercom/` folder were never modified, so restoring them is only for completeness. If the
   script was never run, step 2 is unnecessary.)
3. **Put the old code back.** In the checkout (or on the box, by whatever merged it): revert the two merge commits, newest first,
   so `custom_components/portico_intercom/` exists again and `portico` is as it was:
   ```
   cd $REPO && git revert -m 1 <merge sha of U1b> && git revert -m 1 <merge sha of U1a>
   ```
   (the maintainer merges the revert PR the same way; the sync job then puts the old tree on the box. If the old code was moved aside in
   section 2 step 6, `mv "$BK/old-code-portico_intercom" $CONFIG/custom_components/portico_intercom` is the quick way.) Check:
   `ls $CONFIG/custom_components/` shows `portico` and `portico_intercom`, and `$CONFIG/custom_components/portico/door` does not exist.
4. **The wheel:** the old driver manifest pinned 0.6.2 and Home Assistant reinstalls the pinned wheel at start; the wheel
   already installed is 0.6.2 and is not touched by any of this.
5. **Start Home Assistant:** `ha core start`; repeat the checks of section 3 with the old names (`"domain":"portico_intercom"` in
   check 1).

## 6. If Home Assistant was started with the new code before the script ran

Nothing is lost, and nothing was deleted: the old entry is an orphan of a missing integration (its entities show `unavailable`),
and Home Assistant offers a repair "integration not found: portico_intercom" whose one-click fix **deletes the entry**: do not
click it. If someone did add the door again by hand, the new entry exists beside the orphan and the script refuses (a duplicate
unique id): delete the new entry in Home Assistant, stop Home Assistant, and run section 2 steps 3 to 7. The script is safe to
run at any time Home Assistant is stopped, however many times.

## 7. Things this runbook could not verify

* `ha backups new` (not run), `ha core logs` flags, and the host name `homeassistant` for the REST calls (the checks of
  section 3 were made from the assistant's side with its own HA access, not from the add-on shell).
* That the add-on shell can take the same `flock` Home Assistant holds on `.ha_run.lock` (it can when the add-on and Home
  Assistant share the config directory on the same host: `flock` is a kernel lock on the file). If the script's running check
  reports nothing while Home Assistant is plainly up, the other checks (the 60 s window, the hub socket) are the net, and `ha core
  stop` before everything is the rule.
* The rollback (section 5): written and reviewed, **never run** (not needed).
* The HACS update path (the repository is not published yet).

## 8. What the window did on 2026-10-24

* the maintainer merged U1a (#174, the fold) and U1b (#175, the migration script, this runbook, the integration test) at about 08:40
  without announcing the window. The sync job put the code on the box (the `portico_intercom` symlink went dangling); the
  running Home Assistant kept the old code, and no restart happened in between, so nothing broke until the window.
* At the maintainer's word the runbook was run at the box shell: rehearsal on a copy; `ha core stop`; the dated copy
  `/homeassistant/unify-backup-2026-10-24_0903` (`core.config_entries`, `core.entity_registry`,
  `portico.gate_sip_peer.<entry_id>`, an empty `portico_intercom/` folder); the real run (`done: 1 door
  entry/entries and 2 registry row(s) migrated.`); the dangling symlink moved into the backup folder; `ha core start` at 09:06.
* Result: the door entry is domain `portico` (version 0.3.0), `entry_type` `door`, entry id and unique id kept, the hub
  running, wheel 0.6.2 not reinstalled; `sensor.call_state` and `sensor.listen_leg` kept their ids (no `_2`); history
  continuous (an `unavailable` gap at 09:03); the push entry loaded.
* The real ring check at 09:09 passed: `ringing` 09:09:12, `answered` 09:09:13 (the old app answered), `idle` 09:09:18;
  gate-sip counters moved as expected and the gate-sip boot id did not change.
* About ten minutes of hands-on time. The rollback was not needed.
