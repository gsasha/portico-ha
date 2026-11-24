# transport-rs real-client interop (DEV-764 slice 4b)

Scripts, not cargo tests: they need a real browser. They drive the **real `portico-transport` binary
over its IPC** with a real browser as the WebRTC offerer and a scripted SIP station as the door, so
the whole path station -> transport -> browser (and back) runs. Results: [../../INTEROP.md](../../INTEROP.md).

| File | What it is |
|---|---|
| `harness.py` | One scenario against one browser. Starts the transport (`-socket`), configures a `gate_sip_server` (or two), runs the station (REGISTER with digest, INVITE with a 16 kHz L16 tone, RTP both ways), serves `page.html`, relays the page's offer / trickled candidates to `webrtc_offer` / `webrtc_ice`, measures with an FFT what the station and the browser hear, and prints a verdict (passed / failed criteria). |
| `page.html` | The offerer page (adapted from `spikes/browser/page.html`): a WebAudio oscillator as the microphone (`hz`), codec preferences (`codecs=default|pcmu|pcmufirst`), trickle modes (`mode=normal|candfirst|candsdp`), `restartIce`, `getStats()` reports every second. |
| `hostile.py` | Scenario G: 37 hostile offers, candidates and answers over the IPC (no browser). The process must survive and keep answering. |
| `run_matrix.sh` | `run_matrix.sh <binary> <firefox|chrome> [scenario ...]`: the scenarios one after the other, one summary line each. |
| `fixtures/` | The real Chrome 124 and Firefox 153 offers captured in S1b (used by `hostile.py` and by `scripts/ipc_smoke.py`). |

## Scenarios

| | What | Pass criteria (all checked by the harness) |
|---|---|---|
| A | plain call, 60 s; the browser offers Opus first, then G.722; wideband tones (browser 5 kHz, station 6 kHz) | answer names `G722` then `PCMU` only; browser receives and sends G.722; the station hears 5 kHz, the browser hears 6 kHz (PCMU cannot carry either); the 200 OK is not before the browser connected; the station is hung up when the browser closes |
| B | the browser offers PCMU only (`setCodecPreferences`) | answer `PCMU` only, PCMU both ways, 1 kHz / 440 Hz tones |
| C | Opus-first offer with PCMU listed before G.722 | the answer follows the OFFERER's order (`PCMU`, `G722`), PCMU both ways |
| D | `restartIce()` at 20 s of a 45 s call | same as A and no audible stall (< 1 s) |
| E | the browser is killed at 15 s (SIGKILL of its process group / `docker kill` of its container) | the station gets a BYE (time measured), the call slot is free (a new INVITE rings), the dead call's session is gone (`webrtc_ice` -> `unknown_session`) |
| F | two doors, two browsers at once | each door's tones reach its own browser and nobody else |
| G | hostile offers over IPC (`hostile.py`) | the process survives; Go's error codes |
| H1 / H2 / H3 | trickle: candidates after the offer / inside the offer / before the offer (the last is answered `unknown_session` like Go) | the call connects in all three |

## Run

No cargo on the host: build the binary in the Rust image (see `../../README.md`), then

```sh
export INTEROP_OUT=/somewhere/outside/the/worktree INTEROP_CACHE=/somewhere/outside/the/worktree
BIN=/path/to/portico-transport
./run_matrix.sh $BIN firefox          # host Firefox, headless, throwaway profile, killed by PGID
./run_matrix.sh $BIN chrome           # zenika/alpine-chrome via `docker run --rm --network host` (no Docker network is created)
python3 -I hostile.py $BIN
python3 -I harness.py --binary $BIN --browser firefox --scenario E     # one scenario, JSON on stdout
```

Plain host processes on 127.0.0.1 and the local interfaces; high ephemeral ports. Firefox needs
`/usr/bin/firefox`; Chrome needs `docker` and the image `zenika/alpine-chrome`. Python 3 standard
library only. Every process is stopped by the exact PID / process group / container name the harness
started, never by name.
