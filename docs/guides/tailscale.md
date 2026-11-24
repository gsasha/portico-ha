# Tailscale: modes, automatic keys, approval

How phones that are away from home join the tailnet: the three modes, minting keys automatically, approving in Tailscale with an OAuth app, and changing the mode later. Tailscale is optional.

Related: [enrolling a phone](enrolling-phones.md), [prerequisites: Tailscale step by step](../getting-started/prerequisites.md#tailscale-for-remote-calling-step-by-step) (the order of the steps, the tailnet policy, and why the machine that carries call media must be a tailnet node), [keys and secrets](../reference/keys-and-secrets.md#4-tailscale-credentials).

New to this? Read the step-by-step in the prerequisites first; this page is the reference for the modes, the credentials and the error messages.

## Changing the Tailscale mode

Configure's menu has **Enroll a device** and **Change the Tailscale enrollment mode**. The change asks the same question with the current mode preselected.
Choosing `auto` always shows its credentials step, so a wrong tailnet or tag can be corrected: the
stored tailnet and tag are filled in, and the secret field shows a row of stars:
stars mean a secret is already stored; leave them to keep it, or type a new one to replace it
(a blank field keeps it too; with no secret stored the field is empty and required). `approve` reuses what is
stored and is saved at once while its OAuth app's secret is in the Store and the redirect URI
registered on it is still the one the current https URL gives. If that URL changed, the OAuth-app step comes back and
says why (Tailscale refuses an unregistered redirect URI); its client secret field shows the same
stars, so after adding the new redirect URI to the app you can keep the stored secret. Anything
missing is asked for as the first time.

The three secret fields (the `auto` credential, the OAuth app's client secret and the one-time
admin token) are password fields, so what you type is masked. The stars are a fixed placeholder
of twelve bullets, whatever the stored secret's length; they are never the secret, and are never
stored, logged or sent to Tailscale (a real secret always starts `tskey-`). A field that still
holds some of the stars, for example a new secret typed after them without clearing them, is
refused. The admin token is stored nowhere, so its field never shows stars. The mode is saved only together with what it needs, the dialog then ends with a
confirmation (open Configure again to enroll a phone), and closing it earlier changes nothing.
Switching away deletes nothing, so you can switch back; removing the device entry still deletes
the OAuth app's secret. Phones already enrolled keep working as they are.

## Tailscale: mint keys automatically

With `auto`, Portico keeps a Tailscale credential in the entry's data and mints a key for every
phone you enroll, with no one approving each phone (contrast **approve in Tailscale**, below).
Each key is single-use, pre-authorized, carries the device tag (suggested for a new answer: `tag:portico-phone`) and
expires one hour after it was made. It is **not** ephemeral: Tailscale removes an ephemeral node
"anywhere normally from 30 to 60 minutes after the last activity"
([ephemeral nodes](https://tailscale.com/docs/features/ephemeral-nodes)), so a phone asleep for an
hour would leave the tailnet and could not rejoin on its spent key. A phone that joined therefore
stays in the tailnet until removed: when you remove a phone in Home Assistant, also remove its
machine in the Tailscale admin console (**Machines**). Portico does not do that for you.

The credential is one of:

- **An OAuth client (recommended).** Admin console > Settings > Trust credentials, select
  **Credential**, then **OAuth**; give it the auth keys scope (`auth_keys`) with **Write**, select
  the device tag as its tag (Tailscale requires tags on keys an OAuth client creates), then
  **Generate credential** and copy the client secret (`tskey-client-…`, shown only once). The scope's
  label in the console may differ from its API name; check your console.
  Following [OAuth clients](https://tailscale.com/docs/features/oauth-clients), Portico exchanges
  the secret at `POST https://api.tailscale.com/api/v2/oauth/token` (client credentials grant) for
  an access token that lasts one hour, and creates the key with it as a Bearer token, naming the
  tag. The client id sent with the secret is the one the secret embeds
  (`tskey-client-<id>-<secret>`); Tailscale's documentation does not spell that layout out, and
  Tailscale's own client sends a placeholder id there, so the server goes by the secret. A secret
  without that shape fails the mint before anything is sent.
- **An API access token** (`tskey-api-…`, generated under Settings > Keys; check your console). It
  is used directly on the keys endpoint, and it **expires** after the 1-90 days chosen when it was
  generated; minting then fails until you enter a new one.

The tag (`tag:` then letters, digits and dashes; the form refuses anything else) must be one the
OAuth client was created with, and it must exist in the tailnet policy file (`tagOwners`, owned so
that this credential may apply it); otherwise Tailscale refuses every key (HTTP 400, the tag "not
permitted"). Do not reuse another app's tag: it carries that app's rules. An entry keeps the tag it
stores until you change it under Configure > **Change the Tailscale enrollment mode** > `auto`.
Before the first phone, the policy must let the tag reach Home Assistant, for example a rule from
`tag:portico-phone` to `ha.example.net:443` (an example; use your own address).
Portico cannot set that up. A failed mint (a refused credential, an unreachable API, an unexpected
answer) shows `tailscale_mint_failed` and creates nothing: no HA user, no QR. The log names the
exception class and HTTP status (a refused token exchange logs `TailscaleRejected`). When Tailscale
refuses the key request or the token exchange with a 4xx whose body is JSON with a string
`message`, one more warning gives the status and that message, for example
`Portico: Tailscale's answer to the key request (HTTP status 400): requested tags [tag:x] are invalid
or not permitted`; the message is cut to 200 characters and anything shaped like a key or token
(`tskey-…`, a Bearer value, a long opaque string) is replaced with `[redacted]`. The request, its
headers, the credential and the access token are never logged, and any other answer adds nothing.

## Tailscale: approve in Tailscale

With `approve`, you (an Owner or Admin of the tailnet) approve each phone on Tailscale's own
consent screen, and the single-use key Tailscale returns goes into the phone's QR. Family phones
need no Tailscale account: the phones are owned by the approving admin, count against that admin's
device quota and are covered by ACL rules that name that admin. This uses Tailscale's
[device provisioning with OAuth apps](https://tailscale.com/docs/features/oauth-apps/device-provisioning),
which Tailscale marks **alpha**.

### Once: the OAuth app

Portico needs a Tailscale OAuth app with the `auth_keys:create:once` scope and Portico's callback
registered as its redirect URI, exactly as the flow shows it:
`<your Home Assistant https URL>/api/portico/tailscale/callback` (the external URL if it is https,
else the internal URL if it is https; plain http is refused, because the redirect carries the
approval code). Only your browser has to reach that URL, not Tailscale's servers.

Creating the app needs a Tailscale API access token of an Owner or Admin (admin console >
Settings > Keys > Generate access token): Tailscale only lets an admin credential create OAuth apps.
That token is needed for this one call; revoke it afterwards. Either let the flow create the app
(**Create the OAuth app for me**: paste the token; it is used for one request and stored nowhere)
or create it yourself and paste the client secret:

```sh
curl -X POST "https://api.tailscale.com/api/v2/tailnet/-/oauth-apps" \
  -u "<api-access-token>:" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Portico (Home Assistant)",
    "redirectUris": ["https://<your-ha-host>/api/portico/tailscale/callback"],
    "scopes": ["auth_keys:create:once"]
  }'
```

The answer's `clientSecret` (`tskey-app-<clientID>-<secret>`) is shown once; paste it into
**I have the OAuth app's client secret**. Portico reads the client id from it (fill in the client id
field only if Tailscale gave you one the secret does not contain). The secret is kept in a private
0600 store (`.storage/portico.tailscale_oauth_app.<entry_id>`), never in the entry's settings, a
diagnostics download or the log; removing the device entry deletes it. The entry stores only the
client id and the registered redirect URI.

### Each phone

Name the phone as usual. The flow then opens Tailscale's consent page in a new tab; sign in as an
admin and approve. Tailscale sends the tab back to Portico's callback, which says "Done", and the
dialog goes on by itself: Portico exchanges the approval for a key, and only then creates the
phone's Home Assistant user and shows the QR. The key in the QR is single-use and expires one hour
after the approval: scan it within the hour and let the phone join once.

Nothing is created if you decline, close the dialog, or Tailscale refuses: the dialog shows why.
`tailscale_exchange_rejected` (Tailscale answered 4xx) means the OAuth app is gone, its secret is
stale, or the redirect URI registered on it is not exactly the one Portico showed; fix the app, or
choose another mode under Configure > **Change the Tailscale enrollment mode**. If Tailscale shows
its own error page instead of a consent screen, the client id or redirect URI does not match.

The callback (`GET /api/portico/tailscale/callback`) needs no Home Assistant login: it is a
browser redirect from Tailscale. It accepts only a `state` this Home Assistant issued for an open
enrollment dialog, at most once and within ten minutes, and otherwise changes nothing and shows a
fixed error page. It never redirects and never echoes what it was sent. Home Assistant does not
write the callback URL to its log (it keeps `aiohttp.access` at WARNING); a reverse proxy in front
of it may, but the code in it is single-use, short-lived and useless without the client secret.
