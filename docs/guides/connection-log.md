# Connection log and diagnostics

The phone app's connection reports (DEV-705): the three commands, validation, storage and the journal lines.

## Connection log (DEV-705)

The phone app sends one small report per connection sequence (a network change, an app start, a reconnect, to the moment it
is connected or gives up) so the owner can debug connection timings. `custom_components/portico/connlog.py` keeps them.

- `portico/connlog/report {reports: [...]}`: an enrolled phone only; at most 50 reports and 128 KB per message; replies
  `{stored, duplicates, rejected}`. Errors: `unauthorized`, `invalid_format`, `too_large`, `rate_limited` (600 reports per user
  per ten minutes). The sender is the authenticated HA user, never a field of the message.
- `portico/connlog/get {since_seq?, limit?}`: administrators only; the entries after a cursor (at most 500), the next cursor,
  the store's size. An entry is `{seq, received_ms, phone, report}`, never the HA user id.
- `portico/connlog/clear`: administrators only.

A report is validated again here, whatever the app sent: only known keys, numbers, booleans and short strings (control
characters removed; anything that looks like a URL or a long token becomes `<redacted>`), bounded depth and length, 4 KB at
most each. Kept in a private Store (`.storage/portico.connlog`) for 30 days, 3000 reports and 4 MB, saved with a delay.
Nothing logs a report. An old plugin answers `unknown_command`, and the app then stops sending for that process.

**Journal lines (`kind: "journal"`).** The same command also carries the app's `live` (live video) and `audio` (call audio)
Journal lines, so the owner can read their results from Home Assistant without the user copying the Journal. An item of
`reports` that has `"kind": "journal"` is `{kind: "journal", id, app?, lines: [{t, line}]}`: `id` is a unique string of 1 to 64
of `A-Za-z0-9_-` (the dedup key), `app` the app version, `t` the line's own time in epoch milliseconds (the time of day the
Journal prints in front of a line is left off), `line` the text. At most 50 lines per item and 240 characters per line (the
rest are dropped). An item with no `kind` is a connection report and is handled exactly as before; an old plugin rejects a
journal item (it has no `v`), which the app counts as sent. Kept apart, in a ring of its own (30 days, 20000 lines, 4 MB: at least 24 h of two phones, DEV-764), so
chatty live lines never push connection reports out and the reverse. The sender is the authenticated phone, never a field of
the item (any `phone` or `user_id` in it is ignored), and nothing but counts is sent back to the phone. Validation is strict
and per line: the first word must be `live` or `audio`; only printable ASCII (and the app's own `…` from a shortened id); and
a line that looks like it carries a URL (`://`), an address (IPv4, IPv6, MAC, a host name), `@`, `Bearer`, `tskey-`, a
`token=`/`password=`-style key, or a long hex or base64 run is dropped. Every dropped line is counted (the reply, and a running
total in the store). Replies keep their three counts and add `journal: {lines, dropped}` only when the message held an item;
an item with no acceptable line counts as `rejected`. Limits: `rate_limited` also fires at 3000 Journal lines per user per
ten minutes (an item counts as one report too). `portico/connlog/get` takes the additive filters `kind` (`journal` or
`connection`), `phone` (the enrolled phone's name), `since_ms` (received at or after) and `journal_since_seq` (the Journal's
own cursor); with none of them the reply is today's, plus `journal: {entries, next_seq, total, lines, bytes, dropped}` once
the ring holds anything (`kind: connection` never has it, `kind: journal` always does and leaves `reports` empty). An entry is
`{seq, received_ms, phone, app, id, lines: [{t, line}]}`. `portico/connlog/clear` empties both, replying
`{removed}` plus `removed_journal` when the ring held anything.

**Ring lines, crashes and exit reasons (DEV-762).** The Journal word list also takes `push`, `ws`, `ui`, `hold`, `house`,
`frames`, `net` and `layout` (the app's ring-decision lines); the per-line filter is unchanged. Two more item kinds ride
`portico/connlog/report`, with the same authentication, the 50-item / 128 KB message caps and the same redaction rules:
`{kind: "crash", id, app?, sdk?, t, up?, model?, rel?, thread?, chain: [class names], frames: ["Class.method:line"]}` (at
least one of `chain` and `frames`; at most 8 and 12) and `{kind: "exit", id, app?, sdk?, t, up?, age?, model?, rel?, reason,
importance?, phase?, trace?: [lines]}` (`reason` required; at most 30 trace lines, a Go panic tail). `t` is milliseconds
since the epoch. Keys outside these sets are ignored; no exception message is accepted; every string has a closed shape (at
most 240 characters, letters, digits and `_ . $ : ( ) < > -` and space), and a value that looks like a URL, an address, an
`@`, a key, a long hex or token run or a host name is dropped (a required field: the record is refused). They are kept in a
third ring, `diag` (30 days, 200 records, 256 KB), so a crash loop evicts neither Journal lines nor connection reports; at
most 20 per user per ten minutes (the excess is dropped and counted in the reply, not an error); the same signature from
the same user within an hour is stored once with a `count`. A message that carried one of these items is answered with the
usual three counts plus `diag: {records, merged, dropped, rate_limited}`. `portico/connlog/get` takes `kind` `crash`,
`exit` or `diag` (those leave `reports` and `journal` out) and `diag_since_seq` (the ring's own cursor), and answers
`diag: {entries: [{seq, received_ms, last_ms, count, phone, id, record}], next_seq, total, bytes, dropped}` once the ring
holds anything. `portico/connlog/clear` also empties it (`removed_diag`). Nothing changes for an older app, and files and
replies that hold no such record are exactly what they were. An older plugin rejects the new kinds (no `v`), so ship this
plugin first.
