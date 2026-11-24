# transport-rs parity with the Go transport: index

Reference: Go `transport/` at the branch point of `dev-764-rust-skeleton` (main `a52af72`), built with `golang:1.23`. "Smoke" means `scripts/ipc_smoke.py` gave byte-identical output on both binaries. "Unit" means a `cargo test` case.

This page is an INDEX. The detail (the Go behaviour, what the Rust does, why, and the tests) is in one file per subject under [docs/parity/](docs/parity/). **The numbers are stable**: code comments, tests, the review log, the plan and `contract/transport_ipc_v1_parity_map.json` cite them, so a number is never reused or renumbered; a new difference takes the next free number of its range (300 to 369 are Phase R round 1) and is filed in the subject file it belongs to, then added here. Numbers 1 to 9 occur more than once (the smoke script's own differences in `ipc_process.md`, the SIP library's in `sip.md`, and plain numbered lists such as the Go defects and the carried review items): the section column tells them apart, and a citation of a number above 9 is unambiguous. Phase R round 2 uses 400 to 499 (T1: ipc/process/dispatch 400 to 419, rows 400 to 402 used; T2: group B and D, 420 to 439: 420 to 422 and 423 to 425 are used). Phase R round 1 uses 300 to 369 (A 300 to 305, B 310 to 318, C 320 to 329, D 330 to 339, E 340 to 349, F 350 to 359, G 360 to 369). Phase R round 2, T4 (observer and unifi_protect) uses 460 to 479 (460 to 465 used). **443 is withdrawn** (T3 filed it and took it back in round 2, commit 34708c1, "row 443 withdrawn"); the number is not reused (R3A-13).

## Subject files

| File | Subject | Holds |
|---|---|---|
| [docs/parity/ipc_process.md](docs/parity/ipc_process.md) | IPC server and process | the Unix-socket IPC server, framing, dispatch, flags, signals, exit codes, the panic policy, the smoke-script differences and the Go oddities of the first slice; numbered differences: 6 |
| [docs/parity/sip.md](docs/parity/sip.md) | SIP library | `pkg/sip` (`src/sip/`): messages, digest, dialog and SDP, registrar, endpoint and its timers, RTP, jitter buffer and pacer; numbered differences: 42 |
| [docs/parity/gate_sip_server.md](docs/parity/gate_sip_server.md) | gate_sip_server driver | the `gate_sip_server` driver: the driver interface family and the configuration core (3a), the server loop (3b-1) and the outbound call (3b-2); numbered differences: 50 |
| [docs/parity/webrtc_sfu.md](docs/parity/webrtc_sfu.md) | WebRTC and the SFU | `internal/sfu` (the pure audio code, the str0m session) and `internal/call` (the sequencer, the relay registry, `webrtc_*`); numbered differences: 38 |
| [docs/parity/gate_sip_observer.md](docs/parity/gate_sip_observer.md) | gate_sip_observer driver (RETIRED, history only) | the `gate_sip_observer` driver: the poll driver and its listen leg (5a), the claim and the dial (5b), and its HTTP client; numbered differences: 24 |
| [docs/parity/dahua.md](docs/parity/dahua.md) | dahua_sip driver | the `dahua_sip` driver (`pkg/driver/dahua`) and the relay path of `internal/call/relay.go`; numbered differences: 10 |
| [docs/parity/unifi_protect.md](docs/parity/unifi_protect.md) | unifi_protect driver | the `unifi_protect` driver: the events websocket and API client (7a), the talk family and the hub talkback bridge (7b); numbered differences: 58 |
| [docs/parity/wsmedia.md](docs/parity/wsmedia.md) | wsmedia | `internal/wsmedia`: the away phone's audio over the signalling WebSocket joined to a device leg; numbered differences: 11 |
| [docs/parity/house.md](docs/parity/house.md) | house calls | `internal/house`: the house-call bridge, `-house-calls`; numbered differences: 16 |
| [docs/parity/standalone.md](docs/parity/standalone.md) | standalone mode | the standalone/container mode: the TCP IPC, `-listen`, `-sip-from-env`, `-secrets-env`; numbered differences: 16 |
| [docs/parity/packaging.md](docs/parity/packaging.md) | packaging and size | packaging, release and rollback (Phase 4, build side) and the size and memory budget of the release binary; numbered differences: 0 |

Each subject file keeps, in its slice sections, the "Matched" tables, the differences, the Go oddities and defects, the lists of ported tests by Go name, "Not verified", and the mutation checks, in the order the slices were built; the Phase R round 1 differences of the subject (300 to 369) close the file.

## Numbered differences

| # | Subject file | Section | Summary |
|---|---|---|---|
| 1 | [sip](docs/parity/sip.md) | Deliberate differences | blocking calls, goroutines, `func(*IncomingRequest)` handlers, `context.Context` |
| 1 | [sip](docs/parity/sip.md) | Defects found in the Go code (for a later Go-side fix) | `endpoint.go:199-211` `cancelInvite`: the CANCEL's own `200 OK` is treated as a 200 that crossed the CANCEL |
| 1 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Slice 4b must do (independent review of 4a): done in slice 4 | Payload types from str0m: done (difference 62). 2. `policy.rs` vs str0m |
| 2 | [sip](docs/parity/sip.md) | Deliberate differences | timers are package variables (`timerT1`, ...) |
| 2 | [sip](docs/parity/sip.md) | Defects found in the Go code (for a later Go-side fix) | `endpoint.go:267` `readLoop` ends on any read error, including ECONNREFUSED from ICMP on the connected |
| 3 | [sip](docs/parity/sip.md) | Deliberate differences | header values and request lines keep invalid UTF-8 bytes |
| 3 | [sip](docs/parity/sip.md) | Defects found in the Go code (for a later Go-side fix) | `SendRequest` abandoned mid-ring never CANCELs (callers must do it). |
| 4 | [sip](docs/parity/sip.md) | Deliberate differences | a panic in a handler goroutine crashes the process |
| 4 | [sip](docs/parity/sip.md) | Defects found in the Go code (for a later Go-side fix) | `Dial`: a 200 with unparseable SDP gets neither ACK nor BYE. |
| 5 | [sip](docs/parity/sip.md) | Deliberate differences | `JitterBuffer.Out()` is never closed; a reader blocks forever after `Close` |
| 5 | [sip](docs/parity/sip.md) | Defects found in the Go code (for a later Go-side fix) | The SDP host is not validated and is DNS-resolved as given by the network (`SetRemote`). |
| 6 | [sip](docs/parity/sip.md) | Deliberate differences | `SetRemote` twice starts a second pair of loops |
| 6 | [sip](docs/parity/sip.md) | Defects found in the Go code (for a later Go-side fix) | The per-Call-ID queue is unbounded and a retransmitted INVITE is queued and handled again (no |
| 7 | [sip](docs/parity/sip.md) | Deliberate differences | writing to `AudioIn` after the session closed blocks (nobody reads) |
| 7 | [sip](docs/parity/sip.md) | Defects found in the Go code (for a later Go-side fix) | A retransmitted 200 or 487 arriving after `SendRequest` returned is not re-ACKed. |
| 8 | [sip](docs/parity/sip.md) | Deliberate differences | the RTP socket is `ListenUDP("udp", :port)`, dual-stack |
| 8 | [sip](docs/parity/sip.md) | Defects found in the Go code (for a later Go-side fix) | The digest response is compared with `!=`, not in constant time (nonce-gated, 5 min TTL). |
| 9 | [sip](docs/parity/sip.md) | Deliberate differences | OS and DNS error text is Go's (`write udp A->B: write: connection refused`) |
| 9 | [sip](docs/parity/sip.md) | Defects found in the Go code (for a later Go-side fix) | `JitterBuffer::try_recv` (Rust only) returns `None` when a concurrent `recv` holds the lock. |
| 10 | [sip](docs/parity/sip.md) | Deliberate differences | challenge keys that differ only in case (`Realm`, `realm`) are applied in random map order |
| 11 | [sip](docs/parity/sip.md) | Deliberate differences | nonce eviction ties (equal expiry) broken by map iteration order |
| 12 | [sip](docs/parity/sip.md) | Deliberate differences | `AudioSession` unclosed leaks its goroutines |
| 13 | [sip](docs/parity/sip.md) | Deliberate differences | `unicode.IsPrint` decides what `%q` escapes |
| 14 | [sip](docs/parity/sip.md) | Deliberate differences | `Endpoint.Close` closes the fd and interrupts a blocked read |
| 15 | [sip](docs/parity/sip.md) | Deliberate differences | `Registrar.OnStateChange` / `retryWait` plain fields |
| 16 | [sip](docs/parity/sip.md) | Deliberate differences | **CANCEL's own `200 OK` is taken for a 200 that crossed the CANCEL** (`endpoint.go:199-211`, `cancelInvite`) |
| 17 | [sip](docs/parity/sip.md) | Deliberate differences | the read loop returns on **any** read error, so an ICMP "port unreachable" on the connected socket (station |
| 18 | [sip](docs/parity/sip.md) | Deliberate differences | a `SendRequest` abandoned by its caller (hangup mid-ring) sends nothing |
| 19 | [sip](docs/parity/sip.md) | Deliberate differences | `Call.Bye()` mutates the CSeq (pointer receiver, racy) |
| 20 | [sip](docs/parity/sip.md) | Deliberate differences | `AudioSession.Close` closes the fd synchronously |
| 21 | [sip](docs/parity/sip.md) | Deliberate differences | the handler is called on the read-loop goroutine's behalf (a panic kills the process) |
| 22 | [gate_sip_server](docs/parity/gate_sip_server.md) | Deliberate differences | `Driver` is an interface; optional interfaces are type assertions on the value |
| 23 | [gate_sip_server](docs/parity/gate_sip_server.md) | Deliberate differences | `Registry` is a mutable package map filled by `init()` |
| 24 | [gate_sip_server](docs/parity/gate_sip_server.md) | Deliberate differences | `Capabilities` is marshalled by `json.Marshal` |
| 25 | [gate_sip_server](docs/parity/gate_sip_server.md) | Deliberate differences | `callAudio.Out()` is a `<-chan` any number of readers share |
| 26 | [gate_sip_server](docs/parity/gate_sip_server.md) | Deliberate differences | `uplink` selects between `Done` and `In` at random when both are ready |
| 27 | [gate_sip_server](docs/parity/gate_sip_server.md) | Deliberate differences | `AudioClient` reaches `Server` (`c.s.mu`, `s.client`, `hangupCall`) |
| 28 | [gate_sip_server](docs/parity/gate_sip_server.md) | Deliberate differences | `Driver.Server()` returns `*Server`; the glue calls its methods |
| 29 | [gate_sip_server](docs/parity/gate_sip_server.md) | Deliberate differences | `Start` binds the SIP and RTP sockets |
| 30 | [gate_sip_server](docs/parity/gate_sip_server.md) | Deliberate differences | `net.ResolveUDPAddr` resolves a host name with Go's resolver (error |
| 31 | [gate_sip_server](docs/parity/gate_sip_server.md) | Deliberate differences | `strings.ToUpper` (Unicode) before the codec-name prefix tests |
| 32 | [gate_sip_server](docs/parity/gate_sip_server.md) | Deliberate differences | `Frames()`/`attached` hand-offs inside one mutex |
| 33 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | `s.evq` is an unbounded slice filled from the network (every INVITE/CANCEL pair adds events) |
| 34 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | `WriteToUDP` blocks when the send buffer is full |
| 35 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | `sipLoop`/`rtpLoop` return on any non-timeout read error |
| 36 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | `time.AfterFunc` timers |
| 37 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | the `ctx.Done()` watcher calls `d.Stop()`, which stops whichever server is current when the context ends |
| 38 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | `Start` errors carry Go's `net` text |
| 39 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | `log.Printf` stamps the local zone |
| 40 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | `Server.dial` places the call |
| 41 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | `hangupHandler` iterates `registry.all()` (map order) |
| 42 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | `Stats.RTPOut`, `FramesToApp`, ... are fields guarded by `Server.mu` |
| 43 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | one `Server.mu` guards the call, the client and the stats |
| 44 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | BYE and CANCEL end the live call on a Call-ID match alone (`server.go:547-579`, the match at 551 and 562) |
| 45 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | `onRegister` never reads `Expires` (`server.go:447-453`) |
| 46 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | headers are Go strings of the received bytes, echoed unchanged |
| 47 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | `evq` is an unbounded slice, `Notify` blocks the driver |
| 48 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | an INVITE whose SDP media port is over 65535 (or too long for an int64) is answered and no RTP is ever sent |
| 49 | [gate_sip_server](docs/parity/gate_sip_server.md) | Server loop (slice 3b-1): deliberate differences | `shutdown` returns when it has finished |
| 50 | [gate_sip_server](docs/parity/gate_sip_server.md) | Outbound call (slice 3b-2): deliberate differences | a nonce issued to one host is accepted from ANY allowed host while it lives (5 minutes |
| 51 | [gate_sip_server](docs/parity/gate_sip_server.md) | Outbound call (slice 3b-2): deliberate differences | `dial` is a goroutine that cannot be dropped |
| 52 | [gate_sip_server](docs/parity/gate_sip_server.md) | Outbound call (slice 3b-2): deliberate differences | BYE/CANCEL end the call on the Call-ID alone |
| 53 | [gate_sip_server](docs/parity/gate_sip_server.md) | Outbound call (slice 3b-2): deliberate differences | the call's media state is a pointer the dial goroutine keeps using after the slot is cleared |
| 54 | [gate_sip_server](docs/parity/gate_sip_server.md) | Outbound call (slice 3b-2): deliberate differences | `s.dialRing`, `cancelW`, `dialCap`, `dialAtt` are fields the tests write |
| 55 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 3b-2) | `g722Encoder.encode` of a frame with an odd number of samples panics (index out of range, after having |
| 56 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 3b-2) | `Out()` of the uplink buffer is never closed; a reader blocks forever after `Close` |
| 57 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 3b-2) | the answer is pion's |
| 58 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 3b-2) | `Push(seq, payload []byte)` keeps the caller's slice |
| 59 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 3b-2) | `forward` picks between `stop` and a frame at random |
| 60 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 3b-2) | the codec state of a session is touched from goroutines by convention |
| 61 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | the answer is pion's |
| 62 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | the uplink gate compares the wire payload type with the static 0 and 9 |
| 63 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | pion goes `Failed` about 30 s after the last packet; `OnDisconnected` fires on `Failed`/`Closed` |
| 64 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | a session whose browser never connects goes `Failed` (about 30 s) |
| 65 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | no limit on the offer |
| 66 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | no rewrite |
| 67 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | pion gathers from every up interface |
| 68 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | an mDNS (`.local`) remote candidate is handed to pion's resolver |
| 69 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | `AddICECandidate("a=end-of-candidates")` is an error (`failed to parse foundation`) |
| 70 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | goroutines |
| 71 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | `readRemoteTrack` reads every RTP packet from the track |
| 72 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | pion's parse errors |
| 73 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | `HandleAnswer` accepts an answer to the session's own offer |
| 74 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | the process exit drops every session |
| 75 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | `RtpSendState` seq/ts/SSRC |
| 76 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | `drainSenderRTCP`/`drainReceiverRTCP` goroutines |
| 77 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | `JitterBuffer.playoutLoop` is a goroutine started by the first `Push`, so its delay and its ticker start |
| 78 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | no counters |
| 79 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | `getOrCreate` registers the session before `HandleOffer`, sockets exist from the start, no limit on sessions |
| 80 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | process exit drops everything |
| 81 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Differences (continues the numbering of 4a) | pion's goroutines |
| 82 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 4b) | `net/http` client, TLS included |
| 83 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 4b) | `User-Agent: Go-http-client/1.1`, `Accept-Encoding: gzip` |
| 84 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 4b) | the resolver's error text |
| 85 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 4b) | `Capabilities()` may block (with `dial_enabled` on it waits, up to the request timeout + 250 ms, for the |
| 86 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 4b) | `Answer` with `claims_enabled`, `DialCall` with the dial on and gate-sip saying so |
| 87 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 4b) | `fmt.Printf` to stdout for the driver's log lines |
| 88 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 4b) | a panic kills the process and its parent restarts it |
| 89 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 4b) | `strconv.ParseFloat` of a hexadecimal float is exact |
| 90 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 4b) | `json.Unmarshal` folds key case with Unicode rules |
| 91 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 4b) | `net/http` rejects malformed framing |
| 92 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 4b) | `listenSettings` is a plain struct (a `%+v` would print the token) |
| 93 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 5a) | `HangupCall` blocks (up to 3 s) |
| 94 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 5a) | the producer drops the OLDEST downlink frame by receiving from `a.out` itself; `Attach` trims the same way |
| 95 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 5a) | `DialCall` is a goroutine that the caller's context cancels |
| 96 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 5a) | `Answer` hands its request over an unbuffered channel, so a caller that gave up can never have its request |
| 97 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Differences (continues the numbering of 5a) | a panic anywhere kills the process |
| 98 | [dahua](docs/parity/dahua.md) | Differences (continues the numbering of 5b) | a second `Start` leaks the first endpoint and registrar; a second `Stop` returns the socket's |
| 99 | [dahua](docs/parity/dahua.md) | Differences (continues the numbering of 5b) | `mediaHost` accepts any `net.ParseIP` result with `To4() != nil` |
| 100 | [dahua](docs/parity/dahua.md) | Differences (continues the numbering of 5b) | `Relay.Close` sends a BYE every time it is called (the browser's disconnect, the station's BYE through the |
| 101 | [dahua](docs/parity/dahua.md) | Differences (continues the numbering of 5b) | `Dial` before `Start` dereferences a nil endpoint (a panic in a handler goroutine) |
| 102 | [dahua](docs/parity/dahua.md) | Differences (continues the numbering of 5b) | a panic anywhere ends the process |
| 103 | [dahua](docs/parity/dahua.md) | Differences (continues the numbering of 5b) | a browser that leaves while `AnswerDevice` runs is handled by the `gone` flag of `joinCallToBrowser` |
| 104 | [dahua](docs/parity/dahua.md) | Differences (continues the numbering of 5b) | `relayClosingSink.StateChanged(ended)` closes the relay in the reporting goroutine, which then waits for the |
| 105 | [dahua](docs/parity/dahua.md) | Differences (continues the numbering of 5b) | `findPendingRing` visits the drivers in map order |
| 106 | [dahua](docs/parity/dahua.md) | Differences (continues the numbering of 5b) | `errorReporter` is found by a type assertion on the sink |
| 107 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | `scheme`, `ringMinHold`, `ringMaxHold`, `pingInterval`, `pongWait`, `backoffMin`, `backoffMax` |
| 108 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | `net/http`: keep-alive, transparent gzip |
| 109 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | `crypto/tls`, system roots from `crypto/x509` |
| 110 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | the events frame is decoded with `json.Unmarshal`: bytes that are not UTF-8 become U+FFFD, nesting up to 10000 |
| 111 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | a reader goroutine, a ping goroutine and a write mutex |
| 112 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | `http.ReadResponse` parses the upgrade answer (any head size) |
| 113 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | a panic in the watcher goroutine ends the process |
| 114 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | a panic in the sink during an events frame ends the process |
| 115 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | `Stop` waits for the watcher for ever |
| 116 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | a cancelled context makes the talkback open return an error, which clears `answering` |
| 117 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | `OpenRelay` and `AnswerTalkback` call the console's `talkback-session` and start `ffmpeg` |
| 118 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | the dial resolves and tries addresses with happy eyeballs (RFC 8305) |
| 119 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | `nvr_host` is refused only for `/`, `@` and a space; `nvr_api_key` is unchecked |
| 120 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | an `AnswerTalkback` that fails, or whose future is dropped, only clears `answering` |
| 121 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | `Stop` sets `stopped` without ordering against a report in flight |
| 122 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | REST certificate failures all read `x509: certificate signed by unknown authority` (when it is that) |
| 123 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 6) | duplicate or odd framing is tolerated by `net/http` in its own ways |
| 124 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `pacer.Push/Tick` take `time.Time` |
| 125 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `Stop` closes a channel and waits for the loop goroutine |
| 126 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `Start` runs a goroutine on `time.Timer` |
| 127 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `Configure` refuses `protect_host` only for `/`, `@`, space |
| 128 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `exec.LookPath` (stat, then `access(2)` for execute) |
| 129 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `exec.Command`, one goroutine `Wait`s, `Process.Kill` in `Close`/ctx |
| 130 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | the pump blocks in `stdin.Write` for ever on an `ffmpeg` that is alive but not reading |
| 131 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | a panic in any goroutine of the family ends the process |
| 132 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `In()` is a channel of 64; ffmpeg's exit error is `exit status N` |
| 133 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `talkbackSession` has no `Debug` problem |
| 134 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `bufio.Scanner` on ffmpeg's stderr stops at a 64 KiB line, after which nothing reads the pipe |
| 135 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `<-errDone` waits for the stderr reader for ever |
| 136 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `scheme`, `ffmpegBinary`, `talkOrphan`, `guardTimeout`, `talkDialOrphan`, `talkDialMax`, `talkMinHold` |
| 137 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `Close` blocks until both `ffmpeg`s are reaped, including inside `HangupCallWhy` and `Stop` |
| 138 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `net/http` for the guard (keep-alive, `Accept-Encoding: gzip`, `Go-http-client/1.1`) |
| 139 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | the body is read as bytes; `json.Unmarshal` repairs invalid UTF-8 |
| 140 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | a `guard_url` with `user:password@` is sent with `Authorization: Basic`; `https://` works |
| 141 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | the uplink pacer's silence frame is as long as the first frame |
| 142 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `dropsUp` counts a frame the talkback queue did not take |
| 143 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | a cancelled context ends the open with an error, which frees the door |
| 144 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | the same cancelled context leaves the ring answerable until its hold runs out |
| 145 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | a sink that panics ends the process |
| 146 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `Stop` sets `stopped` without ordering against a report in flight |
| 147 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `answerViaTalkback` puts the relay in the registry after the open, even when the browser left meanwhile (the |
| 148 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `Relay.Close` closes the talkback, then the audio, then BYEs |
| 149 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `Driver.Stop` closes the talkback and returns |
| 150 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `unifi_protect`'s `OpenRelay` and the answer always run the talkback |
| 151 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | (superseded by 156) `media_open` for a call a driver holds answered `not_implemented` |
| 152 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | `OpenRelay`/`AnswerTalkback` POST `talkback-session`, THEN look up `ffmpeg` |
| 153 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | the console-supplied codec text is quoted whole into an error |
| 154 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | the call-out cap timer hangs up by id |
| 155 | [unifi_protect](docs/parity/unifi_protect.md) | Differences (continues the numbering of slice 7a) | the default Rust/Go panic report |
| 156 | [wsmedia](docs/parity/wsmedia.md) | Differences (continues the numbering of 7b) | `media_open` joins the phone's leg through `joinCallToBrowser` |
| 157 | [wsmedia](docs/parity/wsmedia.md) | Differences (continues the numbering of 7b) | a panic in `join` crashes the process |
| 158 | [wsmedia](docs/parity/wsmedia.md) | Differences (continues the numbering of 7b) | the close callbacks run synchronously on the closing goroutine, a panic in one kills the process |
| 159 | [wsmedia](docs/parity/wsmedia.md) | Differences (continues the numbering of 7b) | after `Close` the buffered `in` channel still takes 8 frames that nobody reads |
| 160 | [wsmedia](docs/parity/wsmedia.md) | Differences (continues the numbering of 7b) | `send` (the `media_frame` notification) is a blocking call on the downlink goroutine |
| 161 | [wsmedia](docs/parity/wsmedia.md) | Differences (continues the numbering of 7b) | `json.Unmarshal` turns a lone surrogate escape in a string into U+FFFD |
| 162 | [house](docs/parity/house.md) | Differences (continues the numbering of slice 8) | `Bridge.end` closes the legs, writes the line and starts the events goroutine inline |
| 163 | [house](docs/parity/house.md) | Differences (continues the numbering of slice 8) | each `house_leg_state` is a blocking `Notify` from the goroutine that has it |
| 164 | [house](docs/parity/house.md) | Differences (continues the numbering of slice 8) | `CloseAll` is synchronous and closes the bridges one after the other |
| 165 | [house](docs/parity/house.md) | Differences (continues the numbering of slice 8) | the counters read `time.Now()` (wall clock) |
| 166 | [house](docs/parity/house.md) | Differences (continues the numbering of slice 8) | a panic in any goroutine crashes the process |
| 167 | [house](docs/parity/house.md) | Differences (continues the numbering of slice 8) | the IPC handler goroutine cannot be cancelled |
| 168 | [house](docs/parity/house.md) | Differences (continues the numbering of slice 8) | the decoder is `encoding/json` |
| 169 | [house](docs/parity/house.md) | Differences (continues the numbering of slice 8) | the session id is 8 bytes of `crypto/rand` (fallback: `UnixNano`) |
| 170 | [house](docs/parity/house.md) | Differences (continues the numbering of slice 8) | a WebRTC leg is a pion session; `NewSession(legID)` has no interface policy |
| 171 | [house](docs/parity/house.md) | Differences (continues the numbering of slice 8) | `Manager.Frame` returns the leg's error value |
| 172 | [house](docs/parity/house.md) | Differences (continues the numbering of slice 8) | `TestManyCallsLeaveNoGoroutinesBehind` counts goroutines |
| 173 | [house](docs/parity/house.md) | Differences (continues the numbering of slice 8) | `TestConcurrentOpensNeverOvershootTheCap`, `TestOpensRacingCloseAllNeverLeakABridge` race on OS threads |
| 174 | [house](docs/parity/house.md) | Differences (continues the numbering of slice 8) | `TestNoExistingCodeReferencesTheHouseBridge` greps Go sources |
| 175 | [standalone](docs/parity/standalone.md) | Differences (continues the numbering of slice 9) | `serveReconnects` calls `Serve` in the main goroutine: a panic in it ends the container |
| 176 | [standalone](docs/parity/standalone.md) | Differences (continues the numbering of slice 9) | `sipConfigFromEnv` ranges over two maps: with several malformed numeric (or boolean) variables the one named |
| 177 | [standalone](docs/parity/standalone.md) | Differences (continues the numbering of slice 9) | a `-listen` port that is not a number is looked up in `/etc/services` (`smtp`, `http`, ...) |
| 178 | [standalone](docs/parity/standalone.md) | Differences (continues the numbering of slice 9) | `ListenTCP` resolves the address, tests the IP for loopback, then `net.Listen` resolves the string AGAIN |
| 179 | [standalone](docs/parity/standalone.md) | Differences (continues the numbering of slice 9) | the token is compared as bytes; a token or a password that is not UTF-8 is carried as it is (the password is |
| 180 | [standalone](docs/parity/standalone.md) | Differences (continues the numbering of slice 9) | the text of an unparsable `-ipc-allow-from` entry is `netip`'s (`ParseAddr("x"): unable to parse IP`) |
| 181 | [standalone](docs/parity/standalone.md) | Differences (continues the numbering of slice 9) | a connection that is still in its auth window is not the server's current one, so `Close` does not end it |
| 182 | [standalone](docs/parity/standalone.md) | Differences (continues the numbering of slice 9) | Go's `net` enables `TCP_NODELAY` and keep-alive (15 s, 15 s, 9 probes) on every accepted TCP connection |
| 183 | [standalone](docs/parity/standalone.md) | Differences (continues the numbering of slice 9) | a failed read or write is `read tcp 127.0.0.1:8765->10.0.0.5:51234: ...` |
| 184 | [standalone](docs/parity/standalone.md) | Differences (continues the numbering of slice 9) | `json.Marshal` of a `float64` is `5060`, `1e+21`, `9223372036854776000` |
| 185 | [standalone](docs/parity/standalone.md) | Differences (continues the numbering of slice 9) | `TCPOptions` has no `Debug`; a panic text is printed whole |
| 186 | [standalone](docs/parity/standalone.md) | Differences (continues the numbering of slice 9) | `ReadTokenFile` and the `.env` reader read whatever they are pointed at, to the end, and say nothing of a |
| 187 | [standalone](docs/parity/standalone.md) | Differences (continues the numbering of slice 9) | the token from the variable or the token file may hold white space (only the `.env` reader refuses it) |
| 188 | [standalone](docs/parity/standalone.md) | Differences (continues the numbering of slice 9) | a refused TCP connection is not logged |
| 189 | [standalone](docs/parity/standalone.md) | Differences (continues the numbering of slice 9) | the wildcard bind leaves `IPV6_V6ONLY` to the system default (Go forces dual-stack) |
| 200 | [gate_sip_server](docs/parity/gate_sip_server.md) | Outbound call, dial.go (slice 3b-2): matched | ACK first (new branch, the station's To tag), then the codec and media address checks |
| 300 | [ipc_process](docs/parity/ipc_process.md) | Phase R round 1, group A (ipc, main, dispatch, standalone):  | no `recover()`: a panic in a handler ends the process (exit 2) and `hub.py` restarts it |
| 301 | [ipc_process](docs/parity/ipc_process.md) | Phase R round 1, group A (ipc, main, dispatch, standalone):  | `Notify` writes in the calling goroutine, so an event a driver emits inside a handler (`driver_availability` |
| 302 | [standalone](docs/parity/standalone.md) | Phase R round 1, group A (ipc, main, dispatch, standalone):  | the TCP server accepts, checks the token and serves ONE connection at a time in the accepting goroutine |
| 303 | [ipc_process](docs/parity/ipc_process.md) | Phase R round 1, group A (ipc, main, dispatch, standalone):  | an unrecovered panic on the main goroutine exits with 2 |
| 304 | [ipc_process](docs/parity/ipc_process.md) | Phase R round 1, group A (ipc, main, dispatch, standalone):  | a `config` that `json.Unmarshal` rejects (`1e999`) is `bad_params` with the Unmarshal text |
| 305 | [ipc_process](docs/parity/ipc_process.md) | Phase R round 1, group A (ipc, main, dispatch, standalone):  | command-line arguments are bytes; a non-UTF-8 `-socket` names that file |
| 310 | [sip](docs/parity/sip.md) | Phase R round 1, group B (src/sip, pacer) | (R1-4, narrows difference 3's gap): the dialog BYE and the dial ACK echo the peer's bytes. |
| 311 | [sip](docs/parity/sip.md) | Phase R round 1, group B (src/sip, pacer) | (R1-5): the digest client works on bytes. |
| 312 | [sip](docs/parity/sip.md) | Phase R round 1, group B (src/sip, pacer) | (R1-17, corrects difference 14): `Endpoint::close` releases the socket. |
| 313 | [sip](docs/parity/sip.md) | Phase R round 1, group B (src/sip, pacer) | (R1-121, deliberate difference): `send_request` and the CANCEL wait observe `close`. |
| 314 | [sip](docs/parity/sip.md) | Phase R round 1, group B (src/sip, pacer) | (R1-75): out-of-range ports print Go's text |
| 315 | [sip](docs/parity/sip.md) | Phase R round 1, group B (src/sip, pacer) | (R1-76): `cseq_number` clamps |
| 316 | [sip](docs/parity/sip.md) | Phase R round 1, group B (src/sip, pacer) | (R1-124): `Error::Timeout` and `Error::Closed` |
| 317 | [sip](docs/parity/sip.md) | Phase R round 1, group B (src/sip, pacer) | (R1-102, R1-103, R1-122): |
| 318 | [sip](docs/parity/sip.md) | Phase R round 1, group B (src/sip, pacer) | (R1-38, partial): |
| 320 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Phase R round 1, group C: session, call, webrtc (comments R1 | the answer names the stream `portico-<callID>` and the track `audio` (`a=msid:portico-<callID> audio` |
| 321 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Phase R round 1, group C: session, call, webrtc (comments R1 | `Connected()` is `pc.ConnectionState() == Connected` (false while disconnected) |
| 322 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Phase R round 1, group C: session, call, webrtc (comments R1 | `AddICECandidate` accepts any address and any number; `candidate:` is trimmed by pion, `end-of-candidates` is |
| 323 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Phase R round 1, group C: session, call, webrtc (comments R1 | a refused re-offer changes nothing |
| 324 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Phase R round 1, group C: session, call, webrtc (comments R1 | a panic kills the process, hub.py restarts it |
| 325 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Phase R round 1, group C: session, call, webrtc (comments R1 | `HandleAnswer` unmarshals the whole SDP before the state check |
| 326 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Phase R round 1, group C: session, call, webrtc (comments R1 | pion gathers from every interface with `FlagUp` |
| 327 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Phase R round 1, group C: session, call, webrtc (comments R1 | one goroutine reads the track |
| 328 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Phase R round 1, group C: session, call, webrtc (comments R1 | process exit drops everything; a browser that never connected while it held a dahua ring prints nothing |
| 329 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Phase R round 1, group C: session, call, webrtc (comments R1 | `SetRemoteDescription` refuses a media format that is no number 0 to 255 with `strconv.ParseUint` text |
| 330 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 1, group D (gate_sip_server and the driver API | `sipLoop`/`rtpLoop` return silently on their first read error (`server.go:275`) |
| 331 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 1, group D (gate_sip_server and the driver API | `chooseMedia` keeps the received bytes of an rtpmap name and echoes them in the 200's SDP (`media.go:68, 93`) |
| 332 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 1, group D (gate_sip_server and the driver API | a Call-ID is a Go string compared raw (`server.go:688, 703, 934`) |
| 333 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 1, group D (gate_sip_server and the driver API | `lastDropLog` holds at most 256 sources and is never pruned (`server.go:371-377`) |
| 334 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 1, group D (gate_sip_server and the driver API | received RTP is not checked against the negotiated payload type (`server.go:771-812`) |
| 335 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 1, group D (gate_sip_server and the driver API | requests other than REGISTER/INVITE/ACK/BYE/CANCEL/OPTIONS get no response (`server.go:400-411`), and a |
| 336 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 1, group D (gate_sip_server and the driver API | `net.ResolveUDPAddr` looks up `0xcafe` or `0xab.0xcd` (legal DNS names that Go would send to DNS) |
| 337 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 1, group D (gate_sip_server and the driver API | `onRegister` never reads `Expires` (difference 45) |
| 338 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 1, group D (gate_sip_server and the driver API | an unset `sip_listen` host binds a wildcard that Go makes dual-stack |
| 339 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 1, group D (gate_sip_server and the driver API | a dial future cannot be dropped |
| 340 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Phase R round 1, group E (gate_sip_observer) | `net.Dialer` and `http.Transport` connect with `dialParallel` |
| 341 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Phase R round 1, group E (gate_sip_observer) | `http.Transport` polls an `https` URL with `crypto/tls` (system roots, ALPN, HTTP/2 when the server offers it) |
| 342 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Phase R round 1, group E (gate_sip_observer) | a panic kills the process; nothing is left tracked |
| 343 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Phase R round 1, group E (gate_sip_observer) | a panic kills the process; `configure` is replayed and the capabilities are read again |
| 344 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 1, group D (gate_sip_server and the driver API | `HangupCall`, `Answer` and the audio link as in Go |
| 345 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Phase R round 1, group E (gate_sip_observer) | `net/http` response framing and error texts |
| 346 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Phase R round 1, group E (gate_sip_observer) | `textproto` reads header lines ending in a bare LF |
| 347 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Phase R round 1, group E (gate_sip_observer) | `NewRequest` removes an empty port from the Host header (`removeEmptyPort`) |
| 348 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Phase R round 1, group E (gate_sip_observer) | the sink runs on the poll goroutine's own queue goroutine (`ipc.Notify` can block on a stuck socket) |
| 349 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 1, group D (gate_sip_server and the driver API | `VerifyDigestAuthorization` and `AuthorizationFor` read the header bytes as received |
| 350 | [dahua](docs/parity/dahua.md) | Phase R round 1, group F (dahua_sip and unifi_protect, talk  | a `Start` after a `Stop` on the same `Driver` opens a new endpoint (the old one stays referenced |
| 351 | [unifi_protect](docs/parity/unifi_protect.md) | Phase R round 1, group F (dahua_sip and unifi_protect, talk  | `InsecureSkipVerify` accepts any certificate `crypto/x509` parses (a v1 certificate among them), and checks |
| 352 | [unifi_protect](docs/parity/unifi_protect.md) | Phase R round 1, group F (dahua_sip and unifi_protect, talk  | after a media open that failed at the downlink, `session_up` stays true until the next call replaces the |
| 353 | [unifi_protect](docs/parity/unifi_protect.md) | Phase R round 1, group F (dahua_sip and unifi_protect, talk  | `Stop` waits for ever for the watcher, so a `Start` cannot overlap it |
| 354 | [unifi_protect](docs/parity/unifi_protect.md) | Phase R round 1, group F (dahua_sip and unifi_protect, talk  | a panic kills the process (differences 122, 98): no restart |
| 355 | [unifi_protect](docs/parity/unifi_protect.md) | Phase R round 1, group F (dahua_sip and unifi_protect, talk  | `exec.LookPath` finds the binary once per use |
| 356 | [unifi_protect](docs/parity/unifi_protect.md) | Phase R round 1, group F (dahua_sip and unifi_protect, talk  | `x509.SystemCertPool` is cached process-wide |
| 357 | [ipc_process](docs/parity/ipc_process.md) | Phase R round 1, group F (dahua_sip and unifi_protect, talk  | secrets scrubbed from a panic text: not applicable (a panic ends the process) |
| 358 | [unifi_protect](docs/parity/unifi_protect.md) | Phase R round 1, group F (dahua_sip and unifi_protect, talk  | `filepath.SplitList("")` is empty, and `syscall.Signal.String()` names signals 1 to 31 |
| 359 | [unifi_protect](docs/parity/unifi_protect.md) | Phase R round 1, group F (dahua_sip and unifi_protect, talk  | `Post "<url>": dial tcp ...` and the other errors of the talk open are returned raw (scrubbed only in |
| 360 | [house](docs/parity/house.md) | Phase R round 1, group G (wsmedia, house, tests, conformance | on `media_frame` for a house leg the reply is written first and the `first_frame` / `media_frame` |
| 361 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 1, group G (wsmedia, house, tests, conformance | `dial` of a `gate_sip_server` device: the reply with the `out-` id, then `call_state_changed answered` |
| 362 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Phase R round 1, group G (wsmedia, house, tests, conformance | `gate_sip_observer` claim flow: `answer` is granted with `listen_state owner` first, then the reply |
| 363 | [house](docs/parity/house.md) | Phase R round 1, group G (wsmedia, house, tests, conformance | with house calls on, `media_open` for an id that starts `house-` still opens a DOOR leg (only `media_frame` |
| 364 | [wsmedia](docs/parity/wsmedia.md) | Phase R round 1, group G (wsmedia, house, tests, conformance | `routeClose`: `_ = json.Unmarshal(raw, &p)`: a field of the wrong type is skipped, the decoded ones stay |
| 365 | [house](docs/parity/house.md) | Phase R round 1, group G (wsmedia, house, tests, conformance | `closeBridge` closes `from`, then `to`, one after the other |
| 366 | [wsmedia](docs/parity/wsmedia.md) | Phase R round 1, group G (wsmedia, house, tests, conformance | the close callbacks run on the closing goroutine; a second `Close` returns at once |
| 367 | [wsmedia](docs/parity/wsmedia.md) | Phase R round 1, group G (wsmedia, house, tests, conformance | `New` cannot fail for want of a runtime |
| 368 | [wsmedia](docs/parity/wsmedia.md) | Phase R round 1, group G (wsmedia, house, tests, conformance | a panic in a downlink `send` is not recovered (the process ends) |
| 369 | [wsmedia](docs/parity/wsmedia.md) | Phase R round 1, group G (wsmedia, house, tests, conformance | the pump takes the leg's uplink lock with no way out |
| 400 | [standalone](docs/parity/standalone.md) | Phase R round 2, T1 (ipc, process, dispatch) | admission: the source is checked before any place is taken; a source is never refused unread; its wrong-token reply is slowed; the oldest waiting connection is dropped for a newcomer |
| 401 | [standalone](docs/parity/standalone.md) | Phase R round 2, T1 (ipc, process, dispatch) | `bufio.Scanner` hands out the unterminated tail on any read error; the Rust framer only at end of input |
| 402 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Phase R round 2, T1 (ipc, process, dispatch) | `markJoined` accepts a session that has left the table; `mark_joined` refuses it |
| 420 | [sip](docs/parity/sip.md) | Phase R round 2, group B (src/sip, pacer, gostd) | the digest client hashes and echoes challenge bytes as received; Go repairs each invalid UTF-8 byte to U+FFFD (a Go defect) |
| 421 | [sip](docs/parity/sip.md) | Phase R round 2, group B (src/sip, pacer, gostd) | `serde_json` has `float_roundtrip` in the shipped build (matched, tests = shipped) |
| 422 | [sip](docs/parity/sip.md) | Phase R round 2, group B (src/sip, pacer, gostd) | `parse_hex_float` is a limit, not Go's exact algorithm |
| 423 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 2, group D (gate_sip_server and the driver API) | a zoned `sip_listen` fails to start in both with different texts; the peer zone prints as a scope id |
| 424 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 2, group D (gate_sip_server and the driver API) | `0.0.0.0:port` binds IPv4 only (Go: dual-stack `[::]`); a bracketed host name prefers IPv4 |
| 425 | [gate_sip_server](docs/parity/gate_sip_server.md) | Phase R round 2, group D (gate_sip_server and the driver API) | the orphan grace checks and ends the call in one critical section (Go has a microsecond gap) |
| 440 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Phase R round 2, T3 (sfu, house, wsmedia) | the session drops DTLS/RTP/RTCP from an address that is no remote candidate or STUN source of its own; a `handle_input` error is logged once |
| 441 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Phase R round 2, T3 (sfu, house, wsmedia) | one task serves the datagrams and the device audio: a bounded batch of datagrams, then one device frame (Go: a goroutine per direction) |
| 442 | [webrtc_sfu](docs/parity/webrtc_sfu.md) | Phase R round 2, T3 (sfu, house, wsmedia) | the session's stdout lines differ from Go's (no `connecting`, no mime type, `G.722/PCMU` wording) |
| 444 | [house](docs/parity/house.md) | Phase R round 2, T3 (sfu, house, wsmedia) | a restarted `EndTail` waits (at most 2 s) for the first run's leg closes before the counters line and the `lost` events |
| 460 | [unifi_protect](docs/parity/unifi_protect.md) | Phase R round 2, group F (unifi_protect and talk family) | the REST client dials what `net/http` dials (`Hostname()` / `Port()`, an empty port is the scheme default, `Host` without it); the websocket keeps `dialWS` |
| 461 | [unifi_protect](docs/parity/unifi_protect.md) | Phase R round 2, group F (unifi_protect and talk family) | the REST client refuses an unparsable `Content-Length` and a chunk without CRLF (text approximated); a `Transfer-Encoding` that merely contains `chunked` is still accepted |
| 462 | [unifi_protect](docs/parity/unifi_protect.md) | Phase R round 2, group F (unifi_protect and talk family) | `configure` is refused while the driver runs a watcher or holds a call (Go mutates in place) |
| 463 | [unifi_protect](docs/parity/unifi_protect.md) | Phase R round 2, group F (unifi_protect and talk family) | the session guard: JSON white space only, status judged before the body, empty port, `https://` over verified TLS |
| 464 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Phase R round 2, group E (gate_sip_observer) | a connect that times out at the request deadline reads `context deadline exceeded`; an earlier per-address timeout reads `dial tcp a: i/o timeout` (matched) |
| 465 | [gate_sip_observer](docs/parity/gate_sip_observer.md) | Phase R round 2, group E (gate_sip_observer) | `Transfer-Encoding` is the single token `chunked` or an error; ignored on HTTP/1.0 (matched) |
| 470 | [unifi_protect](docs/parity/unifi_protect.md) | DEV-796 D9 | the events websocket refuses (protocol error) a control frame that is not final or carries more than 125 bytes (RFC 6455 5.5) and more than 64 empty continuation frames in a row; Go answers a ping of any size and counts every frame as life. Goldens: the `golden_read_message` cases in `GOLDEN_READ_DIFFERENCES` |
| 471 | [unifi_protect](docs/parity/unifi_protect.md) | DEV-796 D10 | a ring event whose `id` is empty, over 128 bytes or has a character outside `[A-Za-z0-9._:-]` is ignored (Go takes any non-empty id). Goldens: `frame 12`, `13`, `45`, `46`, `69` in `SCEN_DIFFERENCES` |
| 472 | [wsmedia](docs/parity/wsmedia.md) | DEV-796 C2, C6 | `media_open` answers `too_many` at 16 open legs; a `media_frame` `d` over 4096 bytes is `bad_frame` before it is decoded (Go: no cap, decodes first) |
| 473 | [gate_sip_server](docs/parity/gate_sip_server.md) | DEV-796 S1 | an INVITE is accepted only from the host of the current REGISTER binding (IP only, the binding must be inside its TTL); any other allowed host, or any host before a REGISTER or after a de-registration or lapse, gets 403 Forbidden and no ring, call, counter or call slot (Go rings for any host in allow_from). The Go-golden replays (`rehearsal.rs`, `golden_server.rs`) run with the harness seam `set_open_invites` because those dialogues never REGISTER; the rule itself is pinned by the `s1_` tests in `server_parity.rs` |
| 474 | [unifi_protect](docs/parity/unifi_protect.md) | DEV-796 D2 | a talkback sink whose host is a loopback, unspecified or multicast address (v4, v6, v4-mapped v6) or the name `localhost` is refused with `talkback sink host is not allowed` (Go: scheme and host presence only). Any other host stays valid (the inet_aton forms, trailing dot, IPv4-compatible and unbracketed IPv6, `*.localhost` included, DEV-796 follow-up). Goldens: the `valid_sink` cases `rtp://[::1]:7004` and `udp://0.0.0.0:0` of `talk_helpers.json` (`golden_helpers` accepts this message for a loopback, unspecified or multicast host) |
| 475 | [unifi_protect](docs/parity/unifi_protect.md) | DEV-796 D5 | a talkback open that finishes after `stop` (or a `stop` and a `start`) is closed, awaited (bounded) and refused, and an open on a stopped driver asks nobody; `AnswerTalkback` still answers `no_such_call`. Go stores the session with no check |
| 476 | [unifi_protect](docs/parity/unifi_protect.md) | DEV-835 (bell-talk retired) | an EMPTY `guard_url` means "no external guard": `check_session_guard` returns ok and the talkback open goes on (Go: `no guard_url configured, refusing to open a session`). A `guard_url` that IS set behaves as before (only `sessionUp:false` passes; unreachable, slow, malformed and busy refuse). The one-call-per-door rule (`Busy`) still stops Portico opening two sessions itself. Pinned by `golden_talk` (`SCEN_DIFFERENCES`: scenario `no guard_url`, steps 1 and 2) and `guard_refusal_texts` / `guard_refuses_when_unreachable_slow_or_unset` in `talk_media.rs` |

> **Go sources removed (D-20261123-cc1e).** Go paths in this file, such as `transport/...`, `tools/gen_golden/...` and `scripts/golden_check.sh`, name the Go transport and the golden generators as they were at commit `bdd8e61`; they are deleted from `main` and live in git history only. The golden vectors in `tests/golden/` are frozen recordings. See [tests_and_vectors.md](docs/tests_and_vectors.md).
