# Portico documentation

This is the documentation of the Home Assistant side of Portico. Start with the [project README](../README.md) for what Portico is, then read in the order below. Every page opens with a one-paragraph purpose and links to its neighbours.

> These pages were written from the maintainer's own installation (one house, one Dahua door station). Where a page records what was seen there it says so, and anything that was not checked is marked **unverified**. Examples use made-up addresses in `192.168.2.x` and `example.com`. Dated records of one-off operations on that installation are kept apart in [History](#history).

## Reading order

### Getting started

1. [Prerequisites: Home Assistant, the wheel, network and ports, HTTPS, Tailscale, Frigate, push](getting-started/prerequisites.md)
2. [Requirements and installation](getting-started/install.md)
3. [First run: a door, a phone, a first call](getting-started/first-run.md)
4. [AI-guided setup prompt](getting-started/ai-setup-prompt.md): **the easiest way in.** Give it to an AI assistant (or tell it to read https://raw.githubusercontent.com/gsasha/portico-ha/main/docs/getting-started/ai-setup-prompt.md); it walks you through all of the above and the Portico Android app ([portico-app](https://github.com/gsasha/portico-app))

By hand, in this order: [supported hardware](reference/supported-hardware.md), [prerequisites](getting-started/prerequisites.md), [keys and secrets](reference/keys-and-secrets.md), [install](getting-started/install.md), [configure the door station](guides/door-station-dahua.md), [Tailscale](guides/tailscale.md), [enrol a phone](guides/enrolling-phones.md), [set up a wall tablet](guides/wall-tablet.md), [first run](getting-started/first-run.md).

### Guides (one task per page)

| Task | Page |
| :--- | :--- |
| Configure a Dahua door station (SIP settings, checks, rollback) | [door-station-dahua](guides/door-station-dahua.md) |
| Set up a wall tablet as a ring screen | [wall-tablet](guides/wall-tablet.md) |
| Enrol a device (QR, Phones tab) | [enrolling-phones](guides/enrolling-phones.md) |
| How the one-time code and a phone entry work | [enrolment-code-and-phone-entries](guides/enrolment-code-and-phone-entries.md) |
| Phones away from home: Tailscale | [tailscale](guides/tailscale.md) |
| Wake an away phone: push notification (Firebase) | [push-notifications](guides/push-notifications.md) |
| Away call audio | [away-call-audio](guides/away-call-audio.md) |
| What a phone or tablet shows: pages, blocks, actions | [content-pages-and-actions](guides/content-pages-and-actions.md) |
| Call buttons and Talk to the door | [call-buttons-and-talk](guides/call-buttons-and-talk.md) |
| Live video on camera tiles | [live-video](guides/live-video.md) |
| The dashboard card, wall tablet and panel | [dashboard-card-and-panel](guides/dashboard-card-and-panel.md) |
| Elements tab | [elements](guides/elements.md) |
| Settings tab | [settings-tab](guides/settings-tab.md) |
| UniFi Protect ring detection | [unifi-protect-ring-detection](guides/unifi-protect-ring-detection.md) |
| Connection log and diagnostics | [connection-log](guides/connection-log.md) |
| Updating the integration | [updating](guides/updating.md) |

### Architecture

- [Overview and diagram](architecture/overview.md)
- [Components](architecture/components.md)
- [The transport process](architecture/transport-process.md)

### Reference

- [Who may send which websocket command](reference/websocket-auth.md)
- [Call-state and content events (contract 3)](reference/call-state-and-content-events.md)
- [Contract fixtures](reference/contract-fixtures.md)
- [Supported hardware, versions and assumptions](reference/supported-hardware.md)
- [Keys, tokens and secrets: what to create and where it goes](reference/keys-and-secrets.md)
- [Versions and tags](reference/versions-and-tags.md)
- Door drivers and their fields: [`door/README.md`](../custom_components/portico/door/README.md); the integration's own summary: [`custom_components/portico/README.md`](../custom_components/portico/README.md)

### Development

- [Repository layout](development/repository-layout.md)
- [Running the tests](development/running-tests.md)
- [Publishing (HACS)](development/publishing.md)
- [Contributing](../CONTRIBUTING.md)

### History

- [The unification window runbook and rollback](history/unify_runbook.md): the maintainer's own one-off migration of the former `portico_intercom` entry
- [Rust transport cut-over checklist](history/rust-cutover-checklist.md): the switch from the Go to the Rust transport, and the rollback procedure
- [Go wheel release log (0.6.0 to 0.6.10)](history/go-wheel-releases.md)
- The decision record of the websocket authorisation table (HA-M2) is kept with its table: [websocket-auth](reference/websocket-auth.md).
