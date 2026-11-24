//! `unifi_protect_talk` behind the dispatcher, over the real IPC socket (slice 7b): what `main.go`
//! does with the driver (configure and its reply, `external_ring`, `talk_health`, `dial`,
//! `hangup` with its reason, deconfigure) and the notifications Python sees. The hub builds the
//! driver itself, so it talks https to the console (a self-signed certificate; `verify_ssl`
//! defaults to false for this driver, as in Go) and finds `ffmpeg` on `PATH`: this binary's PATH
//! is a temporary directory holding the fake script. Also `TestExternalRingVerb` and
//! `TestTalkHealthVerbIsReadOnlyAndKeyed` against the real driver (the Go tests use it; the ports
//! in `dispatch/` use a fake ExternalRinger).

mod common;

use std::sync::{Arc, OnceLock};
use std::time::Duration;

use common::talk::*;
use common::unifi::*;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use transport_rs::sfu::session::SessionConfig;
use transport_rs::{dispatch, ipc};

static SHIM: OnceLock<TmpDir> = OnceLock::new();

/// PATH is a directory with the fake ffmpeg (and the system's, for the script's `cat`/`sleep`).
fn shim() -> &'static TmpDir {
    SHIM.get_or_init(|| {
        let d = TmpDir::new("talkipc");
        fake_ffmpeg_both(&d);
        std::env::set_var("PATH", format!("{}:/usr/bin:/bin", d.path().display()));
        d
    })
}

struct Client {
    rd: BufReader<tokio::net::unix::OwnedReadHalf>,
    wr: tokio::net::unix::OwnedWriteHalf,
    next: i64,
    notes: Vec<Value>,
    srv: ipc::Server,
    hub: Arc<dispatch::Hub>,
    dir: std::path::PathBuf,
}

async fn start(tag: &str) -> Client {
    shim();
    let dir = std::env::temp_dir().join(format!("talkipc-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("t.sock");
    let srv = ipc::Server::listen(&sock).unwrap();
    let hub = dispatch::register(
        &srv,
        dispatch::HubOptions {
            sfu: SessionConfig::loopback(),
            ..dispatch::HubOptions::default()
        },
    );
    let s = srv.clone();
    tokio::spawn(async move { s.serve().await });
    let c = UnixStream::connect(&sock).await.unwrap();
    let (rd, wr) = c.into_split();
    Client {
        rd: BufReader::new(rd),
        wr,
        next: 1,
        notes: Vec::new(),
        srv,
        hub,
        dir,
    }
}

impl Client {
    async fn raw(&mut self, method: &str, params: Value) -> String {
        let id = self.next;
        self.next += 1;
        let line = format!(
            "{}\n",
            json!({"id": id, "method": method, "params": params})
        );
        self.wr.write_all(line.as_bytes()).await.unwrap();
        loop {
            let mut l = String::new();
            let n = tokio::time::timeout(Duration::from_secs(10), self.rd.read_line(&mut l))
                .await
                .expect("no response")
                .unwrap();
            assert!(n > 0, "connection closed");
            let v: Value = serde_json::from_str(&l).unwrap();
            if v.get("id").and_then(Value::as_i64) == Some(id) {
                return l.trim_end().to_string();
            }
            self.notes.push(v);
        }
    }
    async fn call(&mut self, method: &str, params: Value) -> Value {
        serde_json::from_str(&self.raw(method, params).await).unwrap()
    }
    async fn note(&mut self, method: &str, within: Duration) -> Option<Value> {
        let end = tokio::time::Instant::now() + within;
        loop {
            if let Some(i) = self.notes.iter().position(|n| n["method"] == method) {
                return Some(self.notes.remove(i));
            }
            let mut l = String::new();
            match tokio::time::timeout_at(end, self.rd.read_line(&mut l)).await {
                Ok(Ok(n)) if n > 0 => self.notes.push(serde_json::from_str(&l).unwrap()),
                _ => return None,
            }
        }
    }
    async fn close(self) {
        self.hub.shutdown().await;
        self.srv.close().await;
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

const WIRE: &str = r#"{"InboundAudio":1,"OutboundAudio":2,"DirectCapable":false,"HasRelay":false,"RTSPTemplate":"","ReportsAvailability":true,"CanDial":true}"#;
const WIRE_NO_DIAL: &str = r#"{"InboundAudio":1,"OutboundAudio":2,"DirectCapable":false,"HasRelay":false,"RTSPTemplate":"","ReportsAvailability":true}"#;

fn talk_cfg_for(con: &str, guard: &str, extra: Value) -> Value {
    let mut m = json!({
        "device_id": "ha_event:front", "protect_host": con, "protect_api_key": TEST_KEY,
        "camera_id": "cam1", "guard_url": guard, "downlink_url": "rtsp://127.0.0.1:1/doorbell"
    });
    for (k, v) in extra.as_object().cloned().unwrap_or_default() {
        m[k] = v;
    }
    json!({"driver": "unifi_protect_talk", "config": m})
}

fn bare() -> Value {
    json!({"driver": "unifi_protect_talk", "config": {
        "device_id": "ha_event:front", "protect_host": "h", "protect_api_key": "k", "camera_id": "c"}})
}

fn dev() -> Value {
    json!({"device_id": "ha_event:front"})
}

// The configure reply is Go's Capabilities bytes (TestCapabilitiesReportsAvailability /
// capabilities_golden_test.go for unifi_protect_talk, with and without dial_enabled), the hub keys
// the driver by its device_id, and a second configure is refused.
#[tokio::test]
async fn configure_reply_and_registry() {
    let mut c = start("cfg").await;
    let line = c.raw("configure", bare()).await;
    assert_eq!(line, format!(r#"{{"id":1,"result":{WIRE}}}"#));
    let ans = c.call("configure", bare()).await;
    assert_eq!(ans["error"]["code"], "already_configured", "{ans}");
    assert_eq!(ans["error"]["message"], "ha_event:front");
    // Start reports the door available
    let av = c
        .note("driver_availability", Duration::from_secs(5))
        .await
        .expect("driver_availability");
    assert_eq!(
        av["params"],
        json!({"device_id": "ha_event:front", "available": true, "reason": ""})
    );
    let ans = c.call("deconfigure", dev()).await;
    assert_eq!(ans["result"], json!({"ok": true}), "{ans}");
    let mut off = bare();
    off["config"]["dial_enabled"] = json!(false);
    let line = c.raw("configure", off).await;
    assert!(
        line.ends_with(&format!(r#""result":{WIRE_NO_DIAL}}}"#)),
        "{line}"
    );
    // refusals with Go's words
    let mut bad = bare();
    bad["config"]["gain"] = json!(40);
    let ans = c.call("configure", bad).await;
    assert_eq!(ans["error"]["code"], "configure_failed");
    assert_eq!(
        ans["error"]["message"],
        "unifi_protect_talk: gain must be a whole number from 1 to 16"
    );
    c.close().await;
}

// TestExternalRingVerb, against the real driver
#[tokio::test]
async fn external_ring_verb_against_the_real_driver() {
    let mut c = start("ring").await;
    c.raw("configure", bare()).await;
    let ring = |id: &str| json!({"device_id": "ha_event:front", "call_id": id, "hold": 20});
    let ans = c.call("external_ring", ring("ha-event-1")).await;
    assert_eq!(ans["result"], json!({"ok": true}), "{ans}");
    // the verb must not announce a ring
    assert!(
        c.note("ringing", Duration::from_millis(200))
            .await
            .is_none(),
        "external_ring notified Python"
    );
    // a new press supersedes the unanswered ring: the old one ends, stamped with the door
    let ans = c
        .call(
            "external_ring",
            json!({"device_id": "ha_event:front", "call_id": "ha-event-2"}),
        )
        .await;
    assert_eq!(ans["result"], json!({"ok": true}), "{ans}");
    let st = c
        .note("call_state_changed", Duration::from_secs(5))
        .await
        .expect("the old ring's end");
    assert_eq!(
        st["params"],
        json!({"call_id": "ha-event-1", "device_id": "ha_event:front", "state": "ended"}),
        "{st}"
    );
    for (params, code) in [
        (
            json!({"device_id": "nope", "call_id": "x"}),
            "unknown_device",
        ),
        (json!({"device_id": "ha_event:front"}), "bad_params"),
        (
            json!({"device_id": "ha_event:front", "call_id": "x", "hold": -1}),
            "bad_params",
        ),
    ] {
        let ans = c.call("external_ring", params.clone()).await;
        assert_eq!(ans["error"]["code"], code, "{params}: {ans}");
    }
    // a hang-up with a reason (Python's bell_pressed path) is the reasoned one
    let ans = c
        .call(
            "hangup",
            json!({"call_id": "ha-event-2", "reason": "bell_pressed"}),
        )
        .await;
    assert_eq!(ans["result"], json!({"ok": true}), "{ans}");
    let st = c
        .note("call_state_changed", Duration::from_secs(5))
        .await
        .expect("ended");
    assert_eq!(st["params"]["state"], "ended");
    let h = c.call("talk_health", dev()).await;
    assert_eq!(h["result"]["last_end"], "hung up (bell_pressed)", "{h}");
    // nothing holds the call any more
    let ans = c.call("hangup", json!({"call_id": "ha-event-2"})).await;
    assert_eq!(ans["error"]["code"], "not_implemented", "{ans}");
    c.close().await;
}

// TestTalkHealthVerbIsReadOnlyAndKeyed, against the real driver
#[tokio::test]
async fn talk_health_verb_against_the_real_driver() {
    let mut c = start("health").await;
    c.raw("configure", bare()).await;
    let line = c.raw("talk_health", dev()).await;
    assert_eq!(
        line,
        r#"{"id":2,"result":{"session_up":false,"bytes_to_camera":0,"frames_from_camera":0,"drops_up":0,"drops_down":0,"last_ffmpeg":"","last_end":""}}"#
    );
    for (params, code) in [
        (json!({"device_id": "nope"}), "unknown_device"),
        (json!({}), "bad_params"),
        (json!("x"), "bad_params"),
    ] {
        let ans = c.call("talk_health", params.clone()).await;
        assert_eq!(ans["error"]["code"], code, "{params}: {ans}");
    }
    c.close().await;
}

// A call out through the hub: the dial reply, the three states stamped with the door, the
// hang-up with its reason, the health afterwards; and the refusals Python sees.
#[tokio::test]
async fn a_call_out_through_the_ipc() {
    let (acceptor, _) = self_signed_acceptor();
    let con = FakeProtect::with_tls(Some(acceptor)).await;
    let guard = FakeGuard::new(r#"{"sessionUp":false}"#).await;
    let mut c = start("dial").await;
    let ans = c
        .call(
            "configure",
            talk_cfg_for(&con.host(), &guard.url(), json!({})),
        )
        .await;
    assert_eq!(ans["result"]["CanDial"], true, "{ans}");
    let ans = c.call("dial", dev()).await;
    let call_id = ans["result"]["call_id"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    assert!(
        ans["result"]["ok"] == true && call_id.starts_with("out-"),
        "{ans}"
    );
    let mut states = Vec::new();
    for _ in 0..3 {
        let n = c
            .note("call_state_changed", Duration::from_secs(5))
            .await
            .expect("state");
        assert_eq!(n["params"]["call_id"], call_id.as_str());
        assert_eq!(n["params"]["device_id"], "ha_event:front");
        states.push(n["params"]["state"].as_str().unwrap().to_string());
    }
    assert_eq!(states, ["ringing", "answered", "active"]);
    assert!(
        c.note("ringing", Duration::from_millis(100))
            .await
            .is_none(),
        "an outbound call read as a ring"
    );
    assert_eq!((guard.hits(), con.reqs().len()), (1, 1));
    // a second call out is refused while this one lives
    let ans = c.call("dial", dev()).await;
    assert_eq!(ans["error"]["code"], "dial_failed", "{ans}");
    assert!(
        ans["error"]["message"]
            .as_str()
            .unwrap()
            .ends_with("the door is in a call: busy"),
        "{ans}"
    );
    let h = c.call("talk_health", dev()).await;
    assert_eq!(h["result"]["session_up"], true, "{h}");
    let ans = c
        .call(
            "hangup",
            json!({"call_id": call_id, "reason": "bell_pressed"}),
        )
        .await;
    assert_eq!(ans["result"], json!({"ok": true}), "{ans}");
    let n = c
        .note("call_state_changed", Duration::from_secs(5))
        .await
        .expect("ended");
    assert_eq!(n["params"]["state"], "ended");
    let h = c.call("talk_health", dev()).await;
    assert_eq!(h["result"]["last_end"], "hung up (bell_pressed)", "{h}");
    // the guard says the old intercom holds the sink: refused, nothing reported
    guard.set_body(r#"{"sessionUp":true}"#);
    let ans = c.call("dial", dev()).await;
    assert_eq!(ans["error"]["code"], "dial_failed", "{ans}");
    assert_eq!(
        ans["error"]["message"],
        "unifi_protect_talk: the old intercom holds the talkback session: busy"
    );
    assert!(c
        .note("call_state_changed", Duration::from_millis(150))
        .await
        .is_none());
    // legacy verbs
    for m in ["answer", "open_relay"] {
        let ans = c.call(m, dev()).await;
        assert_eq!(ans["error"]["code"], "not_supported", "{m}: {ans}");
    }
    c.close().await;
}

// R2-28: `verify_ssl: true` is honoured by the talk family: a console with a self-signed
// certificate is refused (the dial fails with the certificate text, no session POST reaches the
// console), and the same console answers with `verify_ssl: false` (the other tests).
#[tokio::test]
async fn verify_ssl_true_refuses_a_self_signed_console() {
    let (acceptor, _) = self_signed_acceptor();
    let con = FakeProtect::with_tls(Some(acceptor)).await;
    let guard = FakeGuard::new(r#"{"sessionUp":false}"#).await;
    let mut c = start("verify").await;
    let ans = c
        .call(
            "configure",
            talk_cfg_for(&con.host(), &guard.url(), json!({"verify_ssl": true})),
        )
        .await;
    assert_eq!(ans["result"]["CanDial"], true, "{ans}");
    let ans = c.call("dial", dev()).await;
    assert_eq!(ans["error"]["code"], "dial_failed", "{ans}");
    assert!(
        ans["error"]["message"]
            .as_str()
            .unwrap()
            .contains("tls: failed to verify certificate"),
        "{ans}"
    );
    assert!(
        con.reqs().is_empty(),
        "a request reached the console: {:?}",
        con.reqs()
    );
    assert!(c
        .note("call_state_changed", Duration::from_millis(150))
        .await
        .is_none());
    c.close().await;
}

// dial_enabled false: the verb is not_supported, nothing is touched
#[tokio::test]
async fn a_call_out_is_refused_when_the_entry_turns_it_off() {
    let con = FakeProtect::new().await;
    let guard = FakeGuard::new(r#"{"sessionUp":false}"#).await;
    let mut c = start("nodial").await;
    c.call(
        "configure",
        talk_cfg_for(&con.host(), &guard.url(), json!({"dial_enabled": false})),
    )
    .await;
    let ans = c.call("dial", dev()).await;
    assert_eq!(ans["error"]["code"], "not_supported", "{ans}");
    assert_eq!((guard.hits(), con.reqs().len()), (0, 0));
    c.close().await;
}

// A hub shutdown with a live call out reaps the ffmpegs
#[tokio::test]
async fn shutdown_with_a_live_call_out_reaps_the_ffmpegs() {
    let (acceptor, _) = self_signed_acceptor();
    let con = FakeProtect::with_tls(Some(acceptor)).await;
    let guard = FakeGuard::new(r#"{"sessionUp":false}"#).await;
    let mut c = start("shut").await;
    c.call(
        "configure",
        talk_cfg_for(&con.host(), &guard.url(), json!({})),
    )
    .await;
    let ans = c.call("dial", dev()).await;
    assert_eq!(ans["result"]["ok"], true, "{ans}");
    // the processes are children of this test: find them by their command (the fake script)
    let ours = |name: &str| -> usize {
        let mut n = 0;
        if let Ok(rd) = std::fs::read_dir("/proc") {
            for e in rd.flatten() {
                let p = e.path().join("cmdline");
                if let Ok(b) = std::fs::read(&p) {
                    let s = String::from_utf8_lossy(&b).replace('\0', " ");
                    if s.contains(name) && s.contains(shim().path().to_str().unwrap()) {
                        n += 1;
                    }
                }
            }
        }
        n
    };
    let before = ours("ffmpeg");
    assert!(before >= 2, "two ffmpegs expected, found {before}");
    c.hub.shutdown().await;
    let end = std::time::Instant::now() + Duration::from_secs(5);
    while ours("ffmpeg") > 0 && std::time::Instant::now() < end {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    assert_eq!(ours("ffmpeg"), 0, "ffmpeg survived the hub's shutdown");
    c.srv.close().await;
    let _ = std::fs::remove_dir_all(&c.dir);
}

// Slice 8 (PARITY.md 156, which supersedes 151): `media_open` for a call a driver holds is no
// longer refused with `not_implemented`; it opens the leg and joins it (here the ring is the
// door's in-process one, whose answer then fails against the unreachable host of `bare()`; the
// joins that carry audio are tests/wsmedia_ipc.rs). The codec and `already_open` checks still
// come first, and the id is free again once the leg is closed.
#[tokio::test]
async fn media_open_for_a_held_call_opens_the_leg_instead_of_refusing() {
    let mut c = start("media").await;
    c.raw("configure", bare()).await;
    c.call(
        "external_ring",
        json!({"device_id": "ha_event:front", "call_id": "c1", "hold": 30}),
    )
    .await;
    let held = c
        .call("media_open", json!({"call_id": "c1", "codec": "l16"}))
        .await;
    assert_eq!(
        held["result"],
        json!({"codec": "l16", "frame_bytes": 1280, "frame_ms": 40}),
        "{held}"
    );
    // a bad codec is still bad_params, a second open still already_open
    let bad = c
        .call("media_open", json!({"call_id": "c2", "codec": "opus"}))
        .await;
    assert_eq!(bad["error"]["code"], "bad_params", "{bad}");
    let again = c
        .call("media_open", json!({"call_id": "c1", "codec": "pcmu"}))
        .await;
    assert_eq!(again["error"]["code"], "already_open", "{again}");
    let free = c
        .call("media_open", json!({"call_id": "nobody", "codec": "l16"}))
        .await;
    assert_eq!(free["result"]["codec"], "l16", "{free}");
    let closed = c.call("media_close", json!({"call_id": "c1"})).await;
    assert_eq!(closed["result"]["ok"], true, "{closed}");
    c.call("hangup", json!({"call_id": "c1", "reason": "t"}))
        .await;
    let after = c
        .call("media_open", json!({"call_id": "c1", "codec": "pcmu"}))
        .await;
    assert_eq!(after["result"]["codec"], "pcmu", "{after}");
    c.close().await;
}
