# Who may send which websocket command

The authorisation table for the `portico/*` websocket commands, and the recorded decision (HA-M2) that the call commands are open to any authenticated Home Assistant user.

## Authorisation table and the HA-M2 decision

One table, `custom_components/portico/auth.py` (`COMMANDS`), says which caller classes (`admin`, `phone`, `dashboard`,
`any`) may send each `portico/*` command.

**Decision (owner, 2026-10-21; first taken 2026-10-20 as Fable review finding HA-M2, #108): the call commands are open to
any authenticated HA user.** They are `answer`, `hangup`, `open_relay`, `keepalive`, `webrtc_offer`, `webrtc_answer`,
`webrtc_ice`, `calls/subscribe` and `doors`. The suggestion to narrow them to administrators, enrolled phones and declared
dashboard users was declined.

History: M6 E3 (contract 3, O8, merged as #117) had narrowed these nine to `admin, phone, dashboard`, unconditionally,
reversing the earlier HA-M2 decision (merged as #108 the same morning); E3 flagged the disagreement and left the
maintainer to confirm it. On 2026-10-21 the maintainer confirmed the opposite: any HA user. That narrowing is reverted.

- Risk accepted: any HA login, administrator or not, and any leaked HA token, can open the gate relay, answer or
  hang up a call and read call state. Entity permissions are not consulted.
- Reasoning: every HA account on this install belongs to a household member who is already trusted with the front
  door and the gate, and a wall tablet's user is neither an administrator nor an enrolled phone.
- Reopen when an untrusted account can exist: a guest or non-household HA user, a login shared outside the
  household, or an account created for a third party. Then narrow those nine rows to `admin, phone, dashboard`
  (`auth.is_dashboard_user` and the `dashboard` class already exist for it) after declaring the tablet's user as a
  dashboard endpoint.

Everything else is as contract 3 set it: `content/get`, `run`, `snapshot`, `live`, `live_ice`, `live_close` and `subscribe` are `admin, phone, dashboard`;
`content/document`, `preview`, `save`, `cameras/info` and `phones/list`, `phones/enroll`, `phones/cancel` (UI split P2), `phones/revoke` and `phones/set` (UI split P3) and the Settings tab's six, `door_status`, `door_settings/get`, `door_settings/set`, `ui_settings/get` and `ui_settings/set` (UI split P4 to P6), are `admin`; `dial` is `admin, phone`; `media_*` and `push/*`
are `phone`. A login with no Portico role gets the nine call commands, `version` and `kiosk/hello`, and nothing else.
