# Trace-diff Go vs Rust (DEV-764 Phase 2 item 4)

**Recorded 2026-11-14 (Phase R round 3, R3B-1) with the Go binary and the Rust release binary built from commit aed8113 (main, the merge of round 2) plus the commits of the branch dev-764-r3-fix that was based on it (they change src/ipc/tcp.rs, src/sfu/session/run.rs, src/gostd/http1.rs and tests, none of them a timing path of these scenarios); the commit named here and in the `provenance` of `deferred_wire_results.json` is the BASE, aed8113, which is in main's history (`contract_runner.py --audit` checks that the named commits are ancestors of HEAD). The Rust binary is a plain `cargo build --release` (`scripts/shipped_gate.sh` prints 0 on it), the helpers `td_go` / `td_rs` are built from the same tree; sha256 of both binaries is in the `provenance`. The round-2 recording (commit 4468213) was of the T5 branch BEFORE its rebase onto T3 and is replaced.** Earlier recordings (commit a3829b3, of a Rust binary that no longer exists) are replaced. `chaos/` (the Go binary SIGSTOPped) was not re-recorded: it measures the Go side only, and `transport/` is identical.

Tools (REMOVED, DEV-828; the recorded results below are history): `scripts/trace_diff.py` (run / report / selftest / chaos / long), `trace_rtp.py`, and the helpers `tools/td_go` and `tools/td_rs`. Still in the tree: `trace_sip.py`, `tracelib.py`, `wire_deferred.py`.
The Go half could not be re-run after the Go transport was deleted (D-20261123-cc1e). The removed files are in git history (commit bdd8e61 or the parent of the DEV-828 PR).
Traces: `conformance/traces/{go1,go2,rs1,rs2,chaos}/` (go1/go2 = two Go runs = the CONTROL, rs1/rs2 = Rust).
Results of the DEFERRED-WIRE cases: `deferred_wire_results.json`. PARITY.md (now an index; the rows live in docs/parity/) is NOT edited (the numbered rows are
mapped by file and line in contract/; adding rows needs a contract/ change): the findings below are proposed rows TD-1..TD-6.

Tolerances: SIP copies +-60 ms of the RFC 3261 T1 schedule (Go control spread < 10 ms); refresh gap 1950-2150 ms;
RTP mean interval +-0.1 ms, p99 +-2 ms, max +-5 ms, drift <= 200 ppm (Go control: std 0.4 ms, drift 1 to 3 ppm);
PCM: 0 LSB between binaries, oracle excess <= 20 (G.711 quantisation); jitter buffers: counts +-2 slots,
recovery <= 1000 ms after the stall.

Result: go1 vs go2 (control) 346 checks 0 failed; go1 vs rs1, go1 vs rs2 and rs1 vs rs2: 347 checks 0 failed each. The long Timer C pair (tcgo, tcrs, `trace_diff.py long`) is reported on its own and its 2 checks FAIL by design: after a CANCEL answered with its own 200 Go sends a spurious ACK and BYE and Rust neither (the documented difference sip 16, case D-TIMER-C-CANCEL-* of `wire_deferred.py`).
A report with a missing scenario or no checks fails (R2-37): `report --pair go1,nosuchlabel` exits 1.
Discrimination: selftest 28 perturbations all detected; a real run with the Go binary SIGSTOPped (chaos) fails 11 and 12 checks.

## Proposed PARITY rows
* TD-1 (Go oddity, Rust better): uplink jitter buffer (20 ms, real `sfu` uplink buffer) after a 300 ms sender pause with
  consecutive sequence numbers: Go keeps glitching more than 1 s after the stall in 11 of 16 runs (single silent slots up
  to 4 s, one run 53), Rust in 0 of 16 (15 silent slots, then clean). Cause: tick phase against the arrival stream after the
  resync (difference 77). The INTEROP "300 ms stall recovers within about 1 s" item is met by Rust at library level.
* TD-2 (Go defect kept in Rust, DEV-770): the 40 ms relay buffer (dahua VTO -> phone) stays muted FOR EVER after a 300 ms
  pause with consecutive sequence numbers (lag of 7 slots is inside the 8-slot resync window, late packets are dropped):
  105 silent slots to the end in both binaries, through the real IPC path too (relay_J3). A 1000 ms pause resyncs (27/26 slots).
  The same buffer under a sender 1.4 % fast never recovers either (jb_relay_K3).
* TD-3: delayed (burst) and lost stalls behave identically in both: J1 5 / 11 slots, J2 8 / 15 slots (relay / uplink).
* TD-4: gate_sip_server dial sends the INVITE once (no Timer A), CANCEL at 20 s, dial_failed at 22 s, BYE of an unattached
  dialled call at 15 s: identical in both. Dahua REGISTER/INVITE/BYE follow T1 doubling 0/0.5/1.5/3.5 s, give up at 5 s,
  retry 5 s x 2^n +-20 %: identical; no 200 retransmit without ACK in either (1 copy).
* TD-5: HTTP framing texts (gate_sip_observer difference 91) compared with the running Go: chunk-size errors identical; Content-Length -1 and
  duplicate Content-Length give different texts in Rust ("malformed HTTP response" / a TLS unexpected-EOF text instead of
  Go's `bad Content-Length "-1"` / `multiple Content-Length headers`). Both refuse; only the text differs.
* TD-6: webrtc_sfu differences 74 and 80: both binaries BYE a held wsmedia call on SIGTERM; Go does not differ for that path. The WebRTC close_notify
  part stays open (needs a real peer).

## Not verifiable here (named)
Real browser/str0m-vs-pion paced WebRTC peer (the sfu uplink is tested as a buffer, not through ICE/DTLS/SRTP; RTP seq/ts/
SSRC assignment of WebRTC (webrtc_sfu difference 75) and sequence wraparound need a real peer: the gss sender starts below 60000 and the
dahua sender at 0, so a wrap is not reachable in a test). Not closed (no cheap fake): ipc_process carried item "responses of in-flight plain handlers can be lost", sip difference 18, gate_sip_server differences 26 and 33, webrtc_sfu difference 64, unifi_protect difference 124, house difference 167. (Rows are named by subject file and number in docs/parity/; this file used to cite line numbers of the PARITY.md that was split.)
