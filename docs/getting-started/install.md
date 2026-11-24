# Requirements and installation

What you need, and how to put the integration into a Home Assistant installation. Portico is alpha software and is not published through HACS yet, so the supported route today is a manual copy.

Next: [first run](first-run.md). Before you start, read the [prerequisites](prerequisites.md) (network, ports, HTTPS, Tailscale, Frigate, push) and [keys and secrets](../reference/keys-and-secrets.md). Prefer to be guided? Give the [AI-guided setup prompt](ai-setup-prompt.md) to an assistant.

## Requirements

- Home Assistant. The test suite runs against **2026.10.2** (the one version tested; see [running the tests](../development/running-tests.md)).
- The integration manifest (`custom_components/portico/manifest.json`) declares the Python requirement `qrcode>=7.4`, the Home Assistant dependency `file_upload`, and the soft dependencies `network` and `frontend`.
- The transport (a Rust binary since wheel 0.7; the Go build it replaced is kept for rollback) is shipped as a pip wheel, `portico_transport`, that the integration installs itself the first time a door starts and only when the installed wheel is older than the pin in `custom_components/portico/door/wheel.py`. A normal restart makes no network call. The wheel pins in that file exist for `x86_64` and `aarch64`. There is no 32-bit ARM (`armv7`) entry in the pin map, so those machines are not supported.
- A door to talk to, for anything beyond the phone side. See [supported hardware](../reference/supported-hardware.md) and, for a Dahua door station, [configuring the station](../guides/door-station-dahua.md).
- The wheel host must be reachable from Home Assistant: see [the transport wheel](prerequisites.md#the-transport-wheel). **In the current source the pins point at an internal host, so a plain install from outside that network cannot fetch the wheel** until the pin moves to a public release (see [publishing](../development/publishing.md)).

## Install by copying the folder

1. Copy the folder into the Home Assistant config directory, or symlink it if you develop from a clone:

   ```
   cp -r custom_components/portico <config>/custom_components/
   # or, from a clone:
   ln -s "$PWD/custom_components/portico" <config>/custom_components/portico
   ```

   There is no `custom_components/portico_intercom` any more: a stale copy of it beside `portico` would load next to the new code and fight it for the hub socket, so move it away.
2. Restart Home Assistant. The first door that starts installs the `portico_transport` wheel (`door/wheel.py`: the pin is a constant in code; Home Assistant's own requirement installer does the work).
3. Continue with [first run](first-run.md).

An install that still has the former `portico_intercom` door entry must run `scripts/migrate_unify.py` with Home Assistant stopped, once (see [the unification runbook](../history/unify_runbook.md): the exact window, the checks and the rollback). A fresh install needs nothing.

## Install through HACS

Not available yet: the repository is not published as a HACS repository. It carries a `hacs.json` now; what else publishing needs is recorded in [publishing](../development/publishing.md).

## Updating

See [updating the integration](../guides/updating.md).
