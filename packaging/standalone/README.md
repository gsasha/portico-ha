# Portico SIP server as a standalone container (DEV-518 W8)

Status: **the container half is built (the image is now the Rust one), nothing deployed, HA cannot use it yet.** The Python half (a `remote` mode on the
`gate_sip_server` entry, W8b) is not written. No compose file is provided beyond the snippet here.

The maintainer, 2026-11-09: "I want to run it in the HA transport process, but an option to set it up manually in a docker
should be available." The default stays the in-process server (W1 to W7). This is the option.

## What it is

The same `portico-transport` binary, same `gate_sip_server` driver, started in a container:

* `-listen host:port` serves the IPC over **TCP** instead of the Unix socket HA's subprocess uses. It stays up when
  the hub disconnects and accepts the reconnect.
* `-sip-from-env` configures and starts one `gate_sip_server` at start-up from `PORTICO_SIP_*` variables. The hub's
  later `configure` for that `device_id` **adopts** it (reply = its capabilities; the hub's config is ignored, so the
  SIP password never leaves the container), and `deconfigure` only detaches (an entry reload in HA must not drop the
  station's registration).

With neither flag the binary is exactly what it was.

`Dockerfile.rust` builds the image around the Rust binary (DEV-764; `packaging/rust/README.md`); it is the only image: the Go `Dockerfile` was deleted with the Go transport (D-20261123-cc1e). Not deployed.

## Why this design (option a) and not a gate-sip look-alike (option b)

Evidence is in the plan addendum (`automation/portico_sip_server_wiring_plan.md`, "W8 addendum"). Short form: a
look-alike of gate-sip's `:8091` `/health` + `:8092` `GATESIP1` socket would need no Python change, but the server side of
listen, claim, resume and dial is about 1,900 lines of Go in gate-sip itself (`listen.go` 466, `dial.go` 479, `audio.go`
675, `peer.go` 323); Portico would carry a second wire protocol just to be read back by its own observer, with a 1 s
polling latency on rings. The IPC the hub already speaks needs ~150 lines of Go here and a connect-instead-of-spawn mode
in `door/hub.py`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PORTICO_SIP_LISTEN` | `127.0.0.1:5060` | where the station's REGISTER/INVITE arrive. Required; no bind by accident |
| `PORTICO_SIP_PASSWORD` or `PORTICO_SIP_PASSWORD_FILE` | none (required) | 8-15 characters (Dahua sends no REGISTER outside that); prefer the file (secret) |
| `PORTICO_SIP_ALLOW_FROM` | empty | comma list of IPs/CIDRs. **Set it to the station address** (the station SIP server address in the Dahua settings points at this host) |
| `PORTICO_SIP_ALLOW_NON_LOOPBACK` | `false` | the second flag for a non-loopback `PORTICO_SIP_LISTEN` (E1's rule: both, or the driver refuses) |
| `PORTICO_SIP_RTP_PORT` | `40000` | the one RTP port; `0` = kernel-assigned (tests only) |
| `PORTICO_SIP_USER`, `_REALM` | `8001`, `VDP` | the station's SIP.UserID and register realm |
| `PORTICO_SIP_RING_SECONDS`, `_ORPHAN_SECONDS` | 45, 15 | as the in-process server |
| `PORTICO_SIP_DIAL_ENABLED` | `false` | W4 outbound dial (Talk to the gate) |
| `PORTICO_SIP_DEVICE_ID` | `gate-sip-server:<listen>` | the id the HA entry names; set it to something stable |
| `PORTICO_IPC_TOKEN` or `-ipc-token-file` | none (required) | shared secret, at least 24 characters, compared in constant time |
| `-secrets-env <path>` | off | **Recommended on a compose host.** Reads secrets from a KEY=VALUE `.env` (for example `/path/to/.env` on the host, mounted read-only) instead of secret files; see below |

### The `.env` route (`-secrets-env`)

Maps exactly four keys and ignores everything else: `GATE_SIP_PASSWORD` -> SIP password (8-15 characters),
`GATE_SIP_USER` -> SIP user (default `8001`), `GATE_SIP_REALM` -> realm (default `VDP`), `GATE_SIP_PEER_TOKEN` -> the IPC token
(at least 24 characters, no whitespace). Anything else is refused with an error that never contains a value.
`GATE_SIP_TOKEN` (the old app's owner token) is never read into any variable: a key is checked against the allow-list before its
value is touched. Parsing is gate-sip's own (trim; skip blank, `#` and lines without `=`; strip one pair of surrounding quotes;
optional `export `), no shell. Precedence: explicit `PORTICO_SIP_*` / `PORTICO_IPC_TOKEN` variables, `PORTICO_SIP_PASSWORD_FILE`
and `-ipc-token-file` all override the file. The file is read once, at start. Mount it read-only; do not use compose `env_file:`
(docker-socket-proxy would leak `Config.Env`).

Flags: `-listen`, `-ipc-token-file`, `-ipc-allow-non-loopback`, `-ipc-allow-from`, `-sip-from-env`, `-host`.

### The guards (all three layers, all tested)

1. **SIP**: loopback bind unless `PORTICO_SIP_ALLOW_NON_LOOPBACK=true`, and then `PORTICO_SIP_ALLOW_FROM` is mandatory
   (the driver's own rule; the container does not weaken it).
2. **IPC**: a token is mandatory; the first line must be `auth`; loopback unless `-ipc-allow-non-loopback` **and**
   `-ipc-allow-from` (two explicit flags); a source outside the list is closed before a byte is read; nothing is
   notified to a connection that has not authenticated. **The IPC is not encrypted**: loopback, a private docker
   network or a tunnel only. A connection is full control of the transport.
3. **Container**: non-root (10001), read-only root filesystem, all capabilities dropped, no-new-privileges, scratch image
   with a single static binary.

## Network (read before choosing)

SIP needs **UDP 5060** and one **RTP UDP port** (default 40000), both inbound from the station, and the container's
host must be the address the station is told (Dahua SIP server address = this host; the in-process server would have
been HA's address). Two ways:

* **Host networking (recommended)**: no NAT in the path; the SDP and Contact addresses are true.
* **Bridge with published UDP ports**: possible, but NAT rewrites what the SDP carries; the rehearsal must be run against it first.

The maintainer's own rules for their network: no Docker network, container or gateway in `10.0.0.0/8`, `172.30.32.0/23` (the HAOS Supervisor range) or `172.30.42.0/24`
(a gate network of theirs); `172.28.x.x` is used for a bridge network if one is declared. Never run a test instance on the LAN address of a
live station; a local smoke test runs with `--network none`.

## Build and smoke test (local only)

    packaging/rust/build_rust_binaries.sh /tmp/portico-rs-out
    docker build -f packaging/standalone/Dockerfile.rust --build-arg ARCH=amd64 -t local/portico-sip-standalone:test /tmp/portico-rs-out
    # inert smoke: no network at all, loopback defaults, a throwaway token
    printf 'smoke-token-0123456789abcdef-xx' > /tmp/tok; printf 's3cretpass' > /tmp/pw
    docker run --rm --network none --read-only --cap-drop ALL \
      -v /tmp/tok:/run/secrets/portico_ipc_token:ro -v /tmp/pw:/run/secrets/portico_sip_password:ro \
      -e PORTICO_SIP_PASSWORD_FILE=/run/secrets/portico_sip_password local/portico-sip-standalone:test

Do not push the image, do not run it bound to a LAN address for a trial.

## How HA points at it (W8b, built)

The Python side is `door/remote_cfg.py`, `door/remote.py` and `TcpTransportClient` in `door/transport.py`; the entry is added from the
menu item "SIP-server door, remote container (advanced)" and nothing connects until it exists. Design as planned:

A `remote` mode on the `gate_sip_server` entry (flat schema: `sip_listen` and `device_id` stay; `remote_host`, `remote_port`
and `remote_token` are added; existing entries unaffected). `door/hub.py` then **connects** instead of spawning:

1. open TCP to `remote_host:remote_port`, send `{"id":1,"method":"auth","params":{"token":...}}`, require a result;
2. `configure {driver: "gate_sip_server", config: {device_id}}` (adopted; the reply carries `CanDial` and friends);
3. on disconnect, retry with backoff and re-`configure` (the hub already re-configures after a restart);
4. unload of the entry sends `deconfigure` (detach only).

Python-side rules: the host is an IP literal that is loopback or RFC 1918, the port is not gate-sip's 8091/8092/8095, the token is at
least 24 characters (stored in the entry, redacted in diagnostics); the entry holds no SIP password. The door is available exactly
while the link is up (and a call in progress ends when it drops); a ring or call change while it is down is **not replayed**. The
link reconnects with 1 s to 30 s backoff, a wrong token at the longest delay. The one-gate rule (`gate_role.py`) treats this door
like the in-process server: with the gate-sip observer entry also able to dial and also up, Talk is refused on both, so during
the trial let only one of them allow dialling (`PORTICO_SIP_DIAL_ENABLED` here, the observer's dial switch there).

The WebRTC leg for browsers is then terminated in the container, so `-host` must be set to the container host's address
and the SFU's UDP ports reachable from the clients. That is the part to prove in a rehearsal, and the reason the in-process
server stays the default.

## Cut-over differences vs the in-process server

* The station's SIP server address becomes the **container host**, not HA's.
* Firewall rules (W6) follow: station to that host UDP 5060 and the RTP port, and that host to the station's media ports; HA host to the IPC port.
* A ring or call state change while the hub is disconnected is not replayed. The house still rings from the doorbell button automation.
* The SIP password lives in the container's secret, not in the HA config entry (differs from decision 3, which keeps it in the entry for the in-process mode).

## Follow-up (not done)

A compose file and secrets on the maintainer's container host, plus W8b in
`portico-ha`. Neither is opened by this change.
