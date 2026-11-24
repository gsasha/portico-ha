# UniFi Protect ring detection (optional)

How a `unifi_protect` door turns a ring event of the console into a Portico call. Off by default.

## unifi_protect ring detection (optional, off by default)

Set **Listen for the doorbell ring** (`ring_events`) on a `unifi_protect` entry and the transport
opens the console's *public* Integration API events websocket,
`wss://<console>/proxy/protect/integration/v1/subscribe/events` (`X-API-KEY`, the same key the
video and talkback calls use), on start. A `ring` event for the configured camera becomes a
`ringing` call (id `unifi-<event id>`); the console's own end of that event, or 60 seconds with no
end, closes it (never sooner than 10 seconds after the ring, so a ring the console closes at once
can still be answered). Answering from the browser opens the console's talkback session and the
ffmpeg transcode only after the browser's WebRTC leg has connected. The private
`/proxy/protect/ws/updates` socket is not used. Left out, the driver makes no connection at all.
The connection reconnects with a doubling backoff (1 s to 1 min), pings every 30 s, honours
`verify_ssl`, and reports the console's reachability as the entry's availability. This is the first
traffic to the console beyond the video and talkback calls: it needs the same go-ahead as those.
Not verified against a real console: how long a real ring event stays open, and that a given
doorbell model emits `ring` on the public stream (HA's own docs say its API-key-only mode does
not offer ring, so confirm on the console before relying on it).
