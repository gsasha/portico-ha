# The contract fixtures

What the files in `contract/` are, which of them the phone app vendors, and which test checks each one.

## Fixture files

`contract/` holds what the integration's parts and the phone app agree on. Files marked "app" are vendored by
the phone app (`portico-app`) and run against its own code, so a change is a version bump on both
sides.

| File | Role | Checked by |
| :--- | :--- | :--- |
| `provider_v1.md` | the provider protocol between the registry and the door side (an internal seam since the fold) | `provider_conformance.py` and `door_provider_conformance.py` in `tests/` |
| `ws_v3.json` | contract 3 (M6 E3, no aliases): every command, every caller, every frame, and each command's authorization class (app) | `test_ws_contract.py` (records it), `test_auth_table.py` |
| `app_commands_v3.txt` | the commands portico-app 0.5.0 sends: a bare list, one a line, sorted, no comments; the app's test checks its code against it (app, sha256 pinned) | `test_ws_contract.py` |
| `ws_media_v2.json` | the away-media frames (`portico/media_*`), by `door_id` | `test_away_media.py` |
| `enroll_v1.json` | DEV-544: the format-2 QR, the redemption request and every answer, the `/auth/token` refresh the app follows afterwards, the limits, the failure wording, and the cases a fake server replays (app, sha256 pinned) | `test_enroll_redeem.py` (replays every case against the real view) |
| `push_v2.json` | FCM data messages (`pv: "2"`) as a phone receives them and the reaction expected, recorded from the builders (app, sha256 pinned) | `test_push_wire.py` (records it) |
| `icons_v1.json` | the icon names the content editor offers, 118 since blocks (app keeps a copy) | `test_content.py`, `test_palette.py` |
| `palette_v1.json` | the eight button colours of blocks, light and dark fill and text (app keeps a copy) | `test_palette.py` (names equal to the validator's, every contrast ratio recomputed) |
