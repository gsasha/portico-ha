# Away call audio

How a phone that is away from home carries call audio over the signalling websocket. Always on.

## Away call audio (always on)

A phone away from home reaches Home Assistant only through its embedded tailnet client, which forwards the signaling WebSocket but not the SFU's UDP media. There is no option for it (removed 2026-11-12; a stored `away_media` option from an older version is ignored). A phone that holds a call's claim may carry the call audio as 40 ms frames (`pcmu`, or `l16`) on that same WebSocket: `portico/media_open`, `portico/media_frame`, `portico/media_close`. The transport binary joins the phone to the device like a browser's WebRTC session (`transport-rs/src/wsmedia.rs`). Only the claiming connection may open it, for that call, and it closes on hang-up, call end, disconnect or 5 s without uplink. Wire cases: `contract/ws_media_v1.json` (replayed by `tests/test_away_media.py`). A door that cannot carry call audio (a ring-only door without talk-back) still answers `away_media_disabled`: the `away_media` flag a door publishes now means that capability and is `true` for every other door.
