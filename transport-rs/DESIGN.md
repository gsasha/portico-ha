# Task and cancellation design (`src/sip/`, DEV-764 slice 2)

This is the plan's "task and cancellation design" item for the `pkg/sip` port. Slice 1 runs a
**current-thread** tokio runtime (`main.rs`), so nothing here may block a thread: every callback
is `async` or non-blocking, and CPU-bound work is short.

## Cancellation

Go passes a `context.Context` (Registrar) or closes a `stop` channel (AudioSession, JitterBuffer)
or closes a socket (Endpoint). Rust has one primitive for all three: `crate::cancel::CancelToken`
(a clonable flag on a `tokio::sync::watch`; `cancel()` is idempotent, `cancelled().await` resolves
once it is set). It is hand-written (30 lines) to avoid `tokio-util`.

* A token is *signalled by its owner and only observed by the tasks it owns*. A task that holds a
  socket selects on `token.cancelled()` next to its I/O, so it ends at the next await point and
  drops the socket (the OS port is released when the last `Arc` goes).
* Dropping is not cancelling, except `AudioSession`, whose `Drop` cancels (Go leaks an
  unclosed session; a Rust one must not outlive its owner). `Endpoint` and `JitterBuffer` need an
  explicit `close()`, as in Go.

## Tasks and ownership

| Go goroutine | Rust task | Owns | Ends when |
|---|---|---|---|
| `Endpoint.readLoop` | one `tokio::spawn` per `Endpoint`, plus a small supervisor task (its handle is kept in `Inner`) that logs a panic of the loop and closes the endpoint | `Arc<Inner>` and the socket handle | `close()` cancels the endpoint token. **Not** a socket read error: Go returns on any read error, this loop survives all of them (ConnectionRefused retries at once, others after 50 ms; PARITY 17). `close()` also takes `Inner`'s socket handle, so the descriptor goes when the loop returns, and empties the handler and pending tables (R1-17) |
| `Endpoint.dispatch` drain goroutine, one per Call-ID with queued work | one task per live key (its handle is not kept: it ends when the queue is empty, and each handler runs in its own task whose `JoinError` the drain task logs) | the key's queue (in `Inner.dq`) | the queue is empty (the key is removed under the lock) |
| inbound handler `h(req)` | **awaited inside the drain task, each in its own `tokio::spawn`** | the `IncomingRequest` | returns; a panic is caught (JoinError) and the queue keeps draining |
| `Registrar.Run` (caller's goroutine) | the caller's future | `CancelToken` passed in | token cancelled: final `Expires: 0`, return `Ok` |
| `SendRequest` (caller's goroutine) | the caller's future | a bounded `mpsc(4)` registered in `pending[branch]`, owned by a `ReqGuard` | final response, error, timeout; **dropping the future abandons it**: the guard unregisters, and for an INVITE that was ringing hands the channel to a detached task that sends CANCEL and ACKs/BYEs whatever answers |
| `JitterBuffer.playoutLoop` | one task, spawned by the first `push` | `Arc<Inner>` | `close()` cancels its token |
| `AudioSession` `forwardJitterOut`, `sendLoop`, `recvLoop` | three tasks | socket `Arc`, the session token | `close()` or `Drop`; `forwardJitterOut` drops the `AudioOut` sender on exit, which is what ends consumers (`recv() -> None`); `close()` is async, joins the tasks and then drops the socket so the port is free on return |

Channels: Go `chan []byte` of capacity 8 become `mpsc::channel(8)`. Producers that Go writes to
with `select { case ch <- x: default: }` use `try_send` (drop when full); `AudioIn` is a plain
awaited `send` (blocking when full), as in Go.

## Ordering and blocking

* Requests for **one Call-ID** run strictly in arrival order (INVITE, CANCEL, BYE); different
  Call-IDs run concurrently; the read loop never runs a handler, so a stuck handler cannot stop
  responses to our own REGISTER/INVITE from being read. Same structure as Go's `dispatch`.
* Handlers are `async` closures. A handler that must call blocking code uses
  `tokio::task::spawn_blocking`; on the current-thread runtime a blocking handler would stall every
  call (this is the Rust translation of the "pion callbacks must not block" rule in
  `internal/call/sequencer.go`).
* `on_provisional` and `on_ringing` callbacks of `send_request`/`dial` run inline on the caller's
  future, as in Go: keep them non-blocking.
* State shared with callbacks (`pending`, `handlers`, `dq`, jitter buffer, nonce store) lives
  behind `std::sync::Mutex`, held only for map operations and never across an `.await`.

## Time

Timers are `tokio::time` (monotonic). Go's package-level timer variables (`timerT1`, ...) become
`Timers` on the endpoint (`Endpoint::set_timers`), so tests shrink them per endpoint instead of
mutating a global. The jitter buffer's tick is `interval_at(now + ptime)` with
`MissedTickBehavior::Skip`, matching `time.Ticker` (drops ticks for a slow receiver). Jitter
tests run on a paused clock (`start_paused`), which makes the scripted sequences exact.

## The gate_sip_server server (`src/driver/gatesipserver/server/mod.rs`, slice 3b-1)

| Go goroutine | Rust task | Owns | Ends when |
|---|---|---|---|
| `Server.sipLoop` | one task | the tokio half of the SIP socket | `shutdown` cancels the stop token; a read error is retried (100 in a row end it) |
| `Server.rtpLoop` | one task | the tokio half of the RTP socket | same |
| `Server.dispatchLoop` | one task | the event queue (bounded, 1024) | stop token; it flushes the queue first, so `ended` is delivered before `shutdown` returns |
| `Server.rtpSender` (per answered call) | one task per call | the call's serial, stop token, media | the call's stop token (`end_cur_locked`) or `shutdown` |
| `time.AfterFunc` (ring limit, orphan grace, registration lapse) | one task per timer | a `CancelToken` kept in the call or the state | its token (cancelled when the call ends or is re-armed) or the sleep elapsing; once elapsed it cannot be cancelled, like `Timer.Stop` |

Sends are plain functions: every datagram goes out through `std::net::UdpSocket::send_to` on a
non-blocking `dup` of the bound socket, so the 100/180 pair, the 200 and a BYE leave in call order
and `attach`, `hangup`, `AudioClient::goodbye` need no `.await`. One `std::sync::Mutex<State>`
guards the call slot, the client, the stats, the registration and the timers; it is never held
across an `.await` (nothing in the file awaits under it) and a poisoned lock is recovered. The only
lock order is state, then the event queue or a client's own lock (both leaves). `AudioClient` holds
a `Weak<dyn ClientHost>` of the server, so a client never keeps the server alive.

`GateSipServer::start` is serialised by a tokio mutex held across the bind; its context watcher is
a task that ends with the server. `Hub::configure` has no drop guard around a started driver
(the former `StartGuard` could never fire, R1-51): a handler future is owned by a detached task
(`Server::run` spawns it) that is never dropped before it finishes, so nothing cancels `configure`
between `start` and the registry.

## The outbound call (`src/driver/gatesipserver/server/dial.rs`, slice 3b-2)

`Server::dial` is a future, not a goroutine: it reserves the call slot, sends the INVITE and
selects on the server's stop token, the caller's `CancelToken`, the ring timeout and a bounded
`mpsc(8)` the SIP read loop fills (`try_send`, a full channel drops the response as Go's
`select/default` does). Everything after the 200 is synchronous (`dial_answered` has no `.await`),
so there is no cancellation point between the ACK and the call being up. The dialog (`OutCall`) is
an `Arc` shared by the slot and the future, with a small lock of its own below the state lock.

| Go goroutine / timer | Rust task | Owns | Ends when |
|---|---|---|---|
| the goroutine in `dial` (the caller's) | the caller's future | the response receiver, a `DialGuard` | an answer, a refusal, the ring timeout, the context, `Stop`; **dropping it** runs the guard: CANCEL, a detached task waits the cancel wait for the final answer (ACK a 487, ACK+BYE a 200), then the slot is freed |
| `time.AfterFunc` cap (300 s) and attach window (15 s) | one `spawn_timer` task each | a `CancelToken` kept in `OutCall` | the token (the call ended: `end_cur_locked` cancels both) or the sleep elapsing |

The two timers read the audio client (`leg`, `closed`) through the client's own atomic and lock,
never the state lock. A CANCEL from the station never ends a call we placed; a BYE must come from
the station's host (difference 52).

## The WebRTC session (`src/sfu/session/mod.rs`, slice 4b)

str0m is sans-IO, so a `Session` is a tokio task plus the helper tasks around it. The task owns the
`Rtc` and is the only thing that touches it.

| Go goroutine / callback | Rust task | Owns | Ends when |
|---|---|---|---|
| pion's internal goroutines (ICE agent, DTLS, SRTP, SCTP) | `session::run`, one task per `Session` | the `Rtc`, the UDP sockets (one per allowed local address), the `UplinkPath`, the `DownlinkEncoder`, the receivers of the command, datagram and `AudioIn` channels | the stop token (`Close`), the peer closing, the `Disconnected` grace elapsing, the connect timeout, a panic |
| (pion reads the socket itself) | one reader task per socket | its socket, the bounded datagram channel (512, `try_send`: a full channel drops the datagram, a lossy-network behaviour; counted in `Stats::udp_dropped`) | the stop token |
| `forwardJitterOut` | `session::forward_jitter_out` | the `UplinkConverter` (G.722 decoder state) | the stop token or the jitter buffer closing |
| `encodeLoop` | the `audio_in` arm of `run` (not a task of its own) | `DownlinkEncoder`, `RtpSendState` timestamp | with `run` |
| `OnConnectionStateChange` | the `Event` arms of `run` | `Shared` (callback lists under a `std::sync::Mutex`, never held across an `.await` or while a callback runs) | |
| (no equivalent) | the supervisor: awaits the `JoinHandle` of `run` | | `run` ended; a panic that escaped every guard becomes `OnDisconnected` once and a closed session |

Channels and bounds: commands from the IPC handlers `mpsc(16)` (they are serialised per call id by
the keyed lane, so at most one is in flight), datagrams `mpsc(512)` with `try_send`, `AudioIn` and
`AudioOut` `mpsc(8)` as in Go (a producer that cannot send drops the frame), the uplink buffer's
`Out` `mpsc(8)` as in 4a. Nothing a peer can send grows a queue without a bound: str0m's own raw
packet queue is drained on every poll.

Timers: str0m's own timeout (`Output::Timeout`), the `Disconnected` grace, and the connect
timeout, all in one `select!` on the earliest of them. The call to `handle_input(Timeout)` is made
only when str0m's timer is the one that fired.

Panics: every call into str0m (`poll_output`, `handle_input`, `Writer::write`, `accept_offer`) is
under `catch_unwind(AssertUnwindSafe(..))`, and `run` itself is supervised through its `JoinHandle`.
The per-session unit is a task, not an OS thread as in the S1b spike: the process already runs a
current-thread runtime for the SIP and RTP of the gate, str0m's work per session is small (50
packets a second each way, G.722 at about 1 ms of CPU per second of audio), and a task needs no
bridge to enter the runtime that the uplink buffer's `tokio::spawn` requires. The cost is that a
str0m call that never returns would stall the runtime thread; none was seen (hostile.py, S1b's
hostile run).

Two decisions recorded in round 2. (R2-91) The one exception to "every call into str0m is under
the guard": `build_rtc` (`RtcConfig::build`, which makes the DTLS certificate) and `ensure_crypto`
run in `Session::with_config` on the caller's stack. Nothing in them depends on the offer or on
any peer input (a fixed config and the process's own crypto provider), so a panic there is a
build or environment fault that the caller's own guard (the IPC handler's) turns into an error
for that one request. (R2-141) The audio channels are handed around as `Arc<tokio::sync::Mutex<
mpsc::Receiver<..>>>` (`Session::audio_out`, `UplinkBuffer`, `wsmedia::Leg::out_rx`) and the lock
is held across `recv().await`: kept, as the Go channels they port are shared by handle; each
has one reader at a time in production, and a second one would just wait on the lock. Moving to an `Option`-taken receiver is a
refactor of every caller for no behaviour; revisit if a second reader ever appears.

The call sequencer (`src/call/sequencer.rs`) and `webrtc::join_call_to_browser` do not know the
`Session` type: they take the `call::ClientLeg` trait (`on_connected`, `on_disconnected`, `input`,
`output`), as Go's `ClientLeg` / `browserLeg` keep `internal/call` free of `internal/sfu`.

## The dahua_sip driver and the relay (`src/driver/dahua/`, `src/call/relay.rs`, slice 6)

One `Start` creates a run: an `Endpoint` to `host:5060`, a `CancelToken` that ends the registrar (also when the hub's context ends: a small watcher task), and a latch (a second token) that the registrar's supervisor sets when it has finished.

| Go goroutine | Rust task | Owns | Ends when |
|---|---|---|---|
| `Registrar.Run` in `Start`'s goroutine | the registrar task, supervised by a second task that awaits its `JoinHandle` | the `Registrar` (and its endpoint clone) | the run token; the supervisor logs the outcome, raises `driver_error` for an error, reports a panic as unavailable and restarts the registrar with a backoff, and sets the latch when it is over for good, so `Stop` never waits for a registrar that is gone |
| the context watcher of `context.WithCancel` | one task selecting on the hub's token and the latch | clones of the two tokens | either |
| `Endpoint.dispatch` handlers (INVITE, CANCEL, BYE) | the endpoint's per-Call-ID drain task (slice 2), each handler in its own spawned task | `Arc<Mutex<RingState>>` (the pending ring, the ring's Call-ID and To-tag, the active call), an `Arc` of the sink | the handler returns; a panic is caught by the dispatcher |
| `Relay.AnswerDevice` / `DialDevice` in `AnswerViaRelayOnceConnected`'s goroutine | the task `answer_once_connected` spawns when the browser leg connects | the request, a `ByeOnDrop` guard from the 200 until the leg is published | the answer returned; a drop or a panic runs the guard (BYE on a task of its own) |
| `relay.Close` in the SIP handler goroutine of the BYE | a task spawned by `RelayClosingSink` (and by the browser leg's disconnect callback) | the relay (an `Arc`) | the BYE answered or timed out |

State and locks: `RingState` behind one `std::sync::Mutex`, never held across an `.await` (the INVITE handler decides under the lock, sends the 180 after it); `Relay` has a `std` mutex for its state and a `tokio` mutex (`closing`) held for the whole of `close`, so a second `close` returns only when the first has sent its BYE. The relay's state is `closed`-aware: `install` refuses to publish a leg after `close` and tears it down. The hub's `RelayRegistry` is a `std` mutex over a map; `close_all` (shutdown) drains it under the lock and closes outside it, bounded by a timeout.

Bounds: nothing a peer sends grows a queue in this code. The driver's events go straight to the hub's sink (`IpcEventSink`, bounded, a ring and the end of a call are never evicted); one INVITE per Call-ID is a single `RingState` slot (a flood of retransmits is one ring: `a_flood_of_invite_retransmits_is_one_ring`); the relay's audio channels are the `AudioSession` ones (`mpsc(8)`, drop when full).

The driver has no port setting; the device port is 5060. `SIP_PORT` (process-wide, `doc(hidden)`) and `Dahua::set_sip_port` (per driver) are the seams Go's package variable `sipPort` is: tests that reach the driver through the hub serialise on a lock of their own.


## Goroutines, channels and contexts versus tokio tasks: the cross-cutting rules (Phase 2, written 2026-11-13)

The sections above describe each slice. This one is the part a reviewer needs to hold in the head
across all of them, written after slice 10, and it is checked against the code, not recalled. The
black-box contract (`contract/transport_ipc_v1.json`) cannot see any of this; the PARITY rows tagged
`TASK` and `PANIC` in `contract/transport_ipc_v1_parity_map.json` are the ones this section explains.

**Runtime.** One `current_thread` tokio runtime (`src/main.rs`). There is one OS thread for the
protocol code, so a task that blocks stalls every call. The consequences are rules, not options:
nothing blocks inside a task (`spawn_blocking` for the few blocking calls, a child process through
`tokio::process`), nothing CPU-heavy runs long between awaits, and an `std::sync::Mutex` is held only
for map operations and never across an `.await` (Go needs the same discipline for different
reasons; Rust enforces it with `Send` bounds and review).

| Go pattern | Rust translation | Why it differs |
|---|---|---|
| `go f()` that nobody joins | `tokio::spawn`, and the `JoinHandle` is kept by an owner (a supervisor task, a `Hub` table, a drop guard) | an unjoined goroutine that panics kills the process; an unobserved tokio task that panics is silent. Every task that carries a call or a registration is observed |
| `context.Context` passed down | `cancel::CancelToken` (a `watch<bool>`), owned by whoever started the work and observed with `select!` next to the I/O | no parent/child tree and no deadlines in the token: a deadline is a `tokio::time` branch of the same `select!`. Cancelling is idempotent |
| `close(stop)` / `select { case <-stop: }` | the same token | one primitive for contexts, stop channels and closed sockets |
| a blocking `conn.Read` interrupted by `conn.Close()` | the read future sits in `select!` with `token.cancelled()` and the close cancels the token | closing an fd under a pending read is not how a Rust socket is shut down |
| `chan T` with a capacity | `mpsc::channel(n)`; `select { case ch <- x: default: }` is `try_send` and drops when full; the awaited send is `send().await` | the capacity is kept where Go has it; where Go used an unbounded slice (`evq`) Rust uses `evqueue` (PARITY 33, 47) |
| a goroutine blocked forever on a channel nobody reads | none: `recv()` returns `None` once the senders are gone | the "leak" rows of PARITY (5, 7, 12, 25, 56) |
| `time.AfterFunc`, `time.Ticker` | a task selecting on the token and `tokio::time::sleep_until` / `interval_at` with `MissedTickBehavior::Skip` | cancelling a timer that already fired is a no-op (PARITY 36); tests use `start_paused` |
| `sync.Mutex` around a struct | `std::sync::Mutex`, never across `.await`; a lock needed across an await is a `tokio::sync::Mutex` and is named for what it serialises (`closing`) | |
| `defer` | a `Drop` guard (`ReqGuard`, `OpenGuard`, `ByeOnDrop`, `ReleaseGuard`, `AudioSession::drop`) | the difference that matters: **a future can be dropped at any `.await`.** A goroutine runs to its end or is stopped by its context; a future owned by an IPC request that the client abandoned just stops. Anything the future owns that must be released (a dial slot, a ringing INVITE that needs a CANCEL) is held by a guard, and the guard may hand the cleanup to a detached task |
| `recover()` | `catch_unwind` around every call into str0m and around the call-carrying tasks; `JoinError` for a task that panics outside it | Go's non-test code has **no** `recover()`: there a panic ends the process and `hub.py` restarts it once. Rust contains the panic to its task, ends that call and reports it (PARITY 4, 88, 97, 102, 113, 114, 131, 145, 157, 166). A panic on the main thread ends the process with exit 2, the code of Go's runtime (PARITY 303; `panic = "unwind"` is pinned in `Cargo.toml`, the default hook is replaced by `panic_hook` so a payload cannot print a secret) |
| goroutine-per-connection | one task per connection, a single connection in unix mode | the unix server serves ONE connection and the process ends when it closes (a Go oddity kept) |

**The IPC server (`src/ipc/`, R3A-14).** The part with the most tasks, so it gets the table the
subsystem sections above have. One server per process; in unix mode one connection, in TCP mode
(`-ipc-listen`) the hub may reconnect and the process keeps running.

| Task | Owner | Holds | Ends when |
|---|---|---|---|
| acceptor (`Server::ensure_acceptor`, `ipc/tcp.rs`) | the gate's `acceptor` handle, started by `serve` and kept running while a connection is served, so a restarted hub is read at once | the listener (a clone of its `Arc`) | `close` (it checks `is_closed` after an accept error); the listener is dropped before `close` returns (`stop_acceptor`). An accept error is logged and retried after 1 s |
| admission task, one per accepted connection that passed the source check (`admit_in_task`) | spawned by the acceptor, not joined | the socket, a `PendingGuard` (one of 4 places, `ADMISSION_SLOTS`), the receiver of its own eviction channel | the `auth` line is checked (a place is freed, the connection is offered to `serve` through the `ready` slot), the 5 s `AUTH_WINDOW` runs out, the connection is evicted (a newcomer beyond 4 drops the OLDEST waiting one; `select!` is `biased` with `admit` ahead of the eviction, R3A-1), or `close` |
| held reply (inside `TcpGate::admit`, the wrong-token branch, `ipc/tcp.rs`) | the admission task that failed a wrong token | a `DelayedGuard` (at most 8 held at once, `MAX_DELAYED`) and the socket, NOT an admission place | the 5 s back-off (`BAD_TOKEN_BACKOFF`, only from the third wrong token of one source in 10 s) ends and the `unauthorized` reply is written, or `close` |
| `ready` slot (`TcpGate::ready`, `ready_sig`) | `offer` fills it, `next_admitted` / `newcomer` read it | at most ONE authenticated connection waiting for `serve` (a newer one replaces a half-open older one) | `serve` takes it |
| read loop (`Server::serve`, the main future in TCP mode `standalone::serve_reconnects`) | the main task | the connection's reader and the `lanes` table (arrival order is taken here, before a task is spawned) | EOF, a read error, a newer admitted connection, or `close` |
| request task (`spawn_run`, `run_keyed`) | spawned by the read loop, one per request; a keyed request first waits for the previous one of its lane | the request, a `Release` guard of its lane (a panic still frees it) | the reply (or the `internal_error` of a panicking handler) is written. The inline `media_*` lane has no task: it runs on the read loop under `guard::catch` |
| sink writer and its supervisor (`IpcEventSink::new`, `dispatch/sink.rs`) | the hub | the notification queue (`evqueue`, bounded) and the write half | neither returns: the supervisor starts the writer again after a panic (the note being written is lost, R1-16); the shutdown sequence flushes the queue before `close`, and the process exit ends both |
| shutdown task (`main.rs`, `Hub::shutdown` on its own task, R2-108) | `main` | the single-flight shutdown sequence | `Hub::shutdown` returns, then `Server::close` |

The write half is shared by the replies and the notifications under one lock with a 5 s write
timeout (as Go); a failed write drops the connection's writer, and the read loop ends on its own.

**Event queues.** Nothing a peer can send grows a queue without bound. The driver and notification
queues are `evqueue`: bounded, evicting the oldest low-value item (a media frame, a state echo) and
never evicting a ring or the end of a call; only `hard` queued critical items refuse a new one. The
IPC writer has a 5 s write timeout, as Go has; a failed write closes the connection.
The one exception is the house event queue (`house::Manager`, an unbounded channel): a call
produces at most 8 events (one `first_frame`, one `attached`, an `sdp`, two `lost` ...) and at most 4
calls exist, so at most about 32 events can be queued; PARITY 163 has the argument (R1-198).

**Shutdown order** (`SIGINT` or `SIGTERM`; `main.rs` spawns a task that calls `Hub::shutdown` and then
closes the IPC server; the main future also calls `Hub::shutdown` when `serve` returns, so a client
disconnect in unix mode takes the same path): (1) `Hub::shutdown` (single-flight: the first call starts the sequence on a task of its own and
every caller waits for it, so a dropped caller cannot cancel it, R2-108) cancels the hub token, so no new
work starts; (2) the house bridge stops (Go's `stopHouse()`, right after the cancel); (3) the WebRTC
sessions are closed and waited for, at most 2 s in all, and then the relays (at most 3 s), so a held
call is hung up toward the station with a BYE and a browser leg gets its close (PARITY 74, 80);
(4) every driver is stopped, all at once in a `JoinSet`, so one unreachable device's de-register does
not delay the others (each driver's own `shutdown` joins its tasks and releases its ports in a task
of its own, PARITY 49); (5) the notifications the drivers emitted while stopping (the `ended` of a live call) are flushed to the socket (`events.flush`, at most 2 s, R1-16); (6) `Server::close` removes the socket and `main` exits 0. Go cancels a
context, stops the house bridge and the drivers and closes the server; it does not wait for sessions
(the process exit drops them).

**What this section does not claim.** That no await point can deadlock: the ported sequences were
run under the real tokio scheduler in tests and in the black-box runner, but there is no model check.
That drop-at-any-await is safe for a future nobody wrote a guard for: the rule is "list what the
future owns", and the review (Phase R) must repeat that list for each new future.
