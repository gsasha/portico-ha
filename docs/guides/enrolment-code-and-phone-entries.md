# The one-time code and phone entries

How a phone redeems the QR's one-time code for its own sign-in (DEV-544), how Home Assistant proves itself to the phone (DEV-798), and what an enrolled phone is in Home Assistant (one config entry).

Related: [enrolling a phone](enrolling-phones.md). Wire shapes: `contract/enroll_v1.json`, `contract/whoami_v1.json`.

## The one-time code (DEV-544)

`code` is 24 random bytes, URL-safe (32 characters). It is good for **10 minutes and one redemption**. Home Assistant
keeps only a keyed digest of it, in memory; a restart of Home Assistant forgets every unredeemed code, and the
installer draws a new QR. Enrolling a phone **with the name of a phone that is still waiting** ("pending", it never
redeemed its code) is "Show a new QR" for it: same HA user, same `enroll_id`, the earlier code dead at once. A pending
phone nobody redeems within **24 hours** is removed, user and entry (a sweep every hour; a repair issue names it).

The phone redeems with an unauthenticated HTTP POST, because it holds no token to authenticate anything with (HA's
websocket accepts only an access token in its `auth` phase, and HA's own sign-in, `/auth/token`, is the same kind of
view):

```
POST /api/portico/enroll/redeem      Content-Type: application/json      (no Authorization header)
{"code": ..., "enroll_id": ..., "installation_id": ..., "nonce": ...}        at most 640 bytes, exactly these keys
200 {"refresh_token", "access_token", "token_type": "Bearer", "expires_in": 86400, "client_id"}
400 {"error": "invalid_or_expired"}        429 {"error": "rate_limited"} + Retry-After
```

**What the app does** (`contract/enroll_v1.json` has every shape, and the cases a fake server replays):

1. Scan: take a format-2 QR (`code`, no `ha_token`); refuse anything else. Make a `nonce` (16 to 64 of `[A-Za-z0-9_-]`) and
   keep it, with the code, encrypted, until the redemption is finished or definitively refused.
2. Reach Home Assistant (directly, or after joining the tailnet with `tailscale_authkey`) and POST the four fields.
3. On `200`: store `refresh_token` and `client_id` (encrypted, never logged), forget the code. On `400`: stop,
   show "This QR has expired or was already used. Ask for a new one in Home Assistant." On `429`: wait `Retry-After`,
   try once more. If the response was lost, send the SAME request (same `nonce`) again within 2 minutes: the same
   refresh token comes back. Another `installation_id` or `nonce` presenting a used code revokes what the first got.
4. Before each websocket connect, if the stored access token has under 5 minutes left, `POST /auth/token`
   (form-encoded `grant_type=refresh_token`, `refresh_token`, `client_id`): HA 2026.10 answers a refresh grant that
   omits `client_id` with `400 invalid_request`. The access token goes into the websocket `auth` message exactly as
   `ha_token` did. `400 invalid_grant` means the phone was removed or signed out: stop, show "This phone was signed out
   of Home Assistant. Enroll it again.", never loop. The refresh token never changes, and Home Assistant itself
   expires one that was not used for **90 days**.

**Home Assistant proves itself (DEV-798, SEC-02; `contract/whoami_v1.json`).** A phone enrolled with a private `http://`
address would otherwise send its refresh token or access token, in clear text, to whatever answers at that address on
the network it is on (a hotel or cafe Wi-Fi with the same private range or `.local` name). So before it sends a token to
a cleartext address it asks:

```
POST /api/portico/whoami      Content-Type: application/json      (no Authorization header)
{"nonce": <16..64 of [A-Za-z0-9_-], fresh per probe>, "hint": <first 8 bytes of SHA-256(refresh token), 16 hex>}
200 {"proof": HMAC-SHA256(key = the refresh token, message = "portico-whoami-v1" NUL nonce NUL hint), 64 hex}
400 {"error": "invalid_or_expired"}        (malformed, unknown hint, revoked token: one answer)
```

The phone computes the same proof from its own refresh token and compares in constant time; on anything else it sends no
token to that address and uses the tailnet. No new secret, no storage and no enrollment change: the token is the phone's
own, and removing the phone removes its refresh token, which ends the proof. It stops impersonation (a host without the
token cannot answer, and the fresh nonce makes a recorded answer useless). It does **not** hide the token sent afterwards
over `http://` from someone who can read the real LAN: only `https://` or the tailnet does. A phone enrolled the old
format-1 way has no refresh token and so no proof. The endpoint has no failure limiter of its own on purpose: nothing in
it can be guessed, and sharing the enrollment limiter would let one phone with a revoked token slow every enrollment.

Limits: five wrong codes cancel that QR (a repair issue). Beyond that a **throttle, never a wall**: after ten
failures from one source, or twenty well-formed wrong codes from anyone, inside a minute, that scope admits one request
every two seconds and answers the rest `429` with `Retry-After: 2` (a throttled request's code is not even looked at,
and is not counted). A malformed body counts toward its own source only, never toward the global window; a wrong
`Content-Type` is a `415` counted nowhere (a web page cannot send `application/json` cross-site without a pre-flight
HA refuses). When a scope turns hot a repair issue says the endpoint is being hit; it clears at the next redemption or
after an hour of quiet. The source is the connected peer, and only for a request with no `X-Forwarded-For`,
`Forwarded` or `X-Real-IP` header: those requests (and sources that do not fit in the bounded table) share one
`proxied` bucket, and the peer, never a header's claim, is what is kept as the token's last-used address. This matters
here: the Tailscale subnet router is a trusted proxy in HA's http config, so HA believes any `X-Forwarded-For` a tailnet
client sends. If the router also rewrites source addresses (not verified on this estate) every phone shares one source.
The view never feeds Home Assistant's IP ban. Every refusal (unknown, wrong, expired, used, cancelled, malformed) is the
same `400`, byte for byte. Nothing secret is logged: a log line carries an outcome word and four hex characters of the
SHA-256 of the `enroll_id`. The Tailscale key a Portico-minted-key QR carries lives 600 s, like the code (an
Approve-in-Tailscale key lives the hour Tailscale fixes).

**Phones enrolled before this version** keep their ten-year token and nothing changes for them. Enrolling such a
phone again (**the same name**) reuses its HA user and `enroll_id`; its old token keeps working until the new QR is
redeemed, then the user's Portico-made long-lived tokens are revoked. There is no in-place upgrade. The flow asks
first (a confirm step naming the phone, when it was enrolled and last seen, and the consequence), because the name
alone decides which phone is replaced; "Show a new QR" for a phone still waiting asks nothing. A phone that
redeemed but whose entry still says waiting (a crash between two saves) is repaired by the hourly sweep, not removed.

## Phone entries

Each enrolled phone is **one config entry** of this integration (unique id `phone:<enroll_id>`), with its own HA
device named after the phone and two sensors, `sensor.<phone name>_push_registration` (its newest push ring notification
registration: `ok`, `never_sent`, `unregistered`, `project_mismatch`, `not_registered`, or `away_ring_off` when the
push ring notification entry is not loaded) and `sensor.<phone name>_last_connection` (when its app last connected: the app
registers on every connect). The entity ids come from the phone's name, never from the entry id. The entry's data
is identity and token facts only, no secret: `ha_user_id`, `name`, `enroll_id`, `enrolled_at`, `state` (`pending`
until the code is redeemed, `enrolled`, or `compromised` after a code was used by two holders; absent reads as
`enrolled`), `pending_since`, `token_kind` (`llat`, the ten-year long-lived token of a phone enrolled before DEV-544;
`refresh`, a refresh token from a redeemed code), `token_expires_at`, `refresh_state` (`none`, `pending`, `active`),
`tailscale_mode`, `tailscale_node`. Its Configure sets the doors the phone is enrolled for (default: every door,
including doors added later); a phone gets a door's sign-in details and call audio only for those.

**Deleting the entry revokes the phone**: its Home Assistant user (when Portico made it: "Portico — <name>", not an
admin, not the owner) and all that user's tokens, its push ring notification registrations, and its content override. The
Tailscale node is not removed (removing it is not automated): a notification names the phone's node, delete it in
the Tailscale admin console (Machines). Deleting the phone's HA user in Settings > People removes its entry.

Phones enrolled before this version were rows (`enrolled_phones`) on the push ring notification entry. At that entry's setup
each row becomes a phone entry once and the row is dropped; until then, and if a row cannot become an entry, it is
still read as an enrolled phone. A restart changes nothing.

The first Configure asks once how phones join the tailnet: **approve in Tailscale** (`approve`,
listed first and the default for a new entry; see [Tailscale](tailscale.md)), Portico mints a tagged key per phone
(`auto`, with a Tailscale credential it keeps; see [Tailscale](tailscale.md)), or no Tailscale key in the QR (`manual`). With
`manual` the phone does not join the tailnet through Portico: portico-app has no field to type a
Tailscale key in, a key reaches it only through the QR. Use it when phones reach Home Assistant
another way (the home network, or the official Tailscale app on the phone); the away path through
Portico's own Tailscale client needs `approve` or `auto`. An `auto` answer whose credentials were
never saved, or an `approve` answer whose OAuth app was never stored, is asked again on its own.
Entries made before `approve` existed keep their mode.
