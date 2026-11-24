# Versions and tags

How the integration and the transport wheel are versioned and released, and where the wheel pin lives.

## Tag families

The integration is versioned by its `manifest.json` `version` and released under one tag family on this
repository; the transport wheel has its own:

| Tag family | Releases | Example |
| :--- | :--- | :--- |
| `portico-v*` | the integration (`custom_components/portico`) | `portico-v0.3.0` (the manifest says 0.3.0; the only `portico-v*` tag in the repository today is `portico-v0.1.0`, so this tag has not been cut) |
| `transport-v*` | the Go binary and its wheel `portico_transport` (release assets), the lineage up to 0.6.10; also a Go ROLLBACK re-release above a Rust version (`transport-v0.9.0`) | `transport-v0.6.10` |
| `transport-rs-v*` | the Rust binary and its wheel `portico_transport` 0.7.x (`packaging/rust/README.md`); the source pins `transport-rs-v0.7.3` | `transport-rs-v0.7.3` |

(The `intercom-v*` family belonged to the former `portico_intercom` integration and stops at `intercom-v0.2.0`.)
The wheel is pinned by release URL in `door/wheel.py` (`WHEEL_PIN_VERSION`, `WHEEL_PIN_URL`), NOT in the manifest, and
`hub.py` carries `MIN_WHEEL_VERSION`: a change that adds an IPC verb the Python relies on bumps the wheel, the pin and
that constant together; a wheel release the Python does not rely on moves the wheel and the pin only (0.5.1, 0.6.0 to 0.6.10 and 0.7.x did). Bumping the wheel is therefore a code change (two constants), not a manifest edit. (`spike-v0.0.1` is the
original packaging spike.)
