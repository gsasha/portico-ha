//! `unifi_protect` behind the dispatcher, over the real IPC socket: what `main.go` does with the
//! driver (configure, deconfigure, hangup, stream_url, the notifications `ringing`,
//! `call_state_changed`, `driver_availability`) and the refusals Python sees. The hub builds the
//! driver itself, so it talks https: the fake consoles serve a self-signed certificate and the
//! configs say `verify_ssl: false`, exactly as an installer's would.

mod common;

use std::sync::Arc;
use std::time::Duration;

use common::unifi::*;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use transport_rs::sfu::session::SessionConfig;
use transport_rs::{dispatch, ipc};

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
    // Since slice 7b the driver installs the real ffmpeg talkback, found on PATH when a session
    // opens. These tests must not depend on the machine having one (or not): PATH is empty for
    // the whole test binary, so `ffmpeg` is never found and never run.
    static NO_PATH: std::sync::Once = std::sync::Once::new();
    NO_PATH.call_once(|| std::env::set_var("PATH", "/nonexistent-portico-test"));
    let dir = std::env::temp_dir().join(format!("unifiipc-{}-{tag}", std::process::id()));
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

const RING_WIRE: &str = r#"{"InboundAudio":1,"OutboundAudio":2,"DirectCapable":false,"HasRelay":true,"RTSPTemplate":"rtsps://{nvr_host}/{camera_id}","ReportsAvailability":true}"#;
const PLAIN_WIRE: &str = r#"{"InboundAudio":1,"OutboundAudio":2,"DirectCapable":false,"HasRelay":true,"RTSPTemplate":"rtsps://{nvr_host}/{camera_id}","ReportsAvailability":false}"#;

fn cfg(host: &str, extra: Value) -> Value {
    let mut m = json!({"nvr_host": host, "nvr_api_key": TEST_KEY, "camera_id": "cam1", "verify_ssl": false});
    for (k, v) in extra.as_object().cloned().unwrap_or_default() {
        m[k] = v;
    }
    json!({"driver": "unifi_protect", "config": m})
}

fn ring(id: &str) -> String {
    format!(
        r#"{{"type":"add","item":{{"id":"{id}","modelKey":"event","type":"ring","start":1,"device":"cam1"}}}}"#
    )
}

// The configure reply is Go's Capabilities bytes (TestCapabilitiesReportsAvailability: unifi_protect
// reports availability only with ring_events), and a ring arrives as the notifications main.go
// sends, the driver_availability first; a hang-up from Python ends it.
#[tokio::test]
async fn configure_reply_the_notifications_of_a_ring_and_the_hangup_verb() {
    let f = FakeConsole::new(true).await;
    let mut c = start("ring").await;
    let line = c
        .raw("configure", cfg(&f.host(), json!({"ring_events": true})))
        .await;
    assert_eq!(line, format!(r#"{{"id":1,"result":{RING_WIRE}}}"#));
    let av = c
        .note("driver_availability", Duration::from_secs(5))
        .await
        .expect("driver_availability");
    assert_eq!(
        av["params"],
        json!({"device_id": f.host(), "available": true, "reason": ""}),
        "{av}"
    );
    f.last_conn().await;
    f.send(&ring("evt1")).await;
    let r = c
        .note("ringing", Duration::from_secs(5))
        .await
        .expect("ringing");
    assert_eq!(r["params"]["device_id"], json!(f.host()));
    assert_eq!(r["params"]["call_id"], "unifi-evt1");

    // hang up through the verb: ok, and the call ends
    let ans = c
        .call("hangup", json!({"call_id": "unifi-evt1", "reason": "test"}))
        .await;
    assert_eq!(ans["result"], json!({"ok": true}), "{ans}");
    let st = c
        .note("call_state_changed", Duration::from_secs(5))
        .await
        .expect("call_state_changed");
    assert_eq!(st["params"]["call_id"], "unifi-evt1");
    assert_eq!(st["params"]["state"], "ended");
    // a call nobody holds: the verb's old answer
    let ans = c.call("hangup", json!({"call_id": "unifi-evt1"})).await;
    assert_eq!(ans["error"]["code"], "not_implemented", "{ans}");

    // a second device for the same host is refused; deconfigure stops the watcher
    let again = c
        .call("configure", cfg(&f.host(), json!({"ring_events": true})))
        .await;
    assert_eq!(again["error"]["code"], "already_configured", "{again}");
    let ans = c.call("deconfigure", json!({"device_id": f.host()})).await;
    assert_eq!(ans["result"], json!({"ok": true}), "{ans}");
    let end = tokio::time::Instant::now() + Duration::from_secs(5);
    while !f.client_frames().iter().any(|(op, _)| *op == 8) {
        assert!(
            tokio::time::Instant::now() < end,
            "the watcher never closed the socket"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    c.close().await;
}

// Without ring_events nothing is watched and nothing is reported.
#[tokio::test]
async fn without_ring_events_nothing_connects() {
    let f = FakeConsole::new(true).await;
    let mut c = start("plain").await;
    let line = c.raw("configure", cfg(&f.host(), json!({}))).await;
    assert_eq!(line, format!(r#"{{"id":1,"result":{PLAIN_WIRE}}}"#));
    assert!(c
        .note("driver_availability", Duration::from_millis(500))
        .await
        .is_none());
    assert_eq!(f.attempt_count(), 0);
    c.close().await;
}

// An unreachable console is reported unavailable with the short cause, no secret in it.
#[tokio::test]
async fn an_unreachable_console_is_reported_unavailable() {
    let f = FakeConsole::new(true).await;
    let host = f.host();
    drop(f);
    tokio::time::sleep(Duration::from_millis(50)).await;
    let mut c = start("down").await;
    let line = c
        .raw("configure", cfg(&host, json!({"ring_events": true})))
        .await;
    assert!(line.contains("\"result\""), "{line}");
    let av = c
        .note("driver_availability", Duration::from_secs(5))
        .await
        .expect("driver_availability");
    assert_eq!(
        av["params"],
        json!({"device_id": host, "available": false, "reason": "console unreachable"}),
        "{av}"
    );
    c.close().await;
}

// stream_url over the socket (TestStreamURLHandler's device, the driver behind it): the minted
// URL, the console's refusal as stream_url_failed with neither key nor body in it.
#[tokio::test]
async fn stream_url_over_the_socket() {
    let (acceptor, _) = self_signed_acceptor();
    let p = FakeProtect::with_tls(Some(acceptor)).await;
    let mut c = start("url").await;
    let line = c.raw("configure", cfg(&p.host(), json!({}))).await;
    assert!(line.contains(PLAIN_WIRE), "{line}");
    let ans = c.call("stream_url", json!({"device_id": p.host()})).await;
    assert_eq!(
        ans["result"],
        json!({"url": "rtsps://nvr.example:7441/TOKEN123?enableSrtp"}),
        "{ans}"
    );
    assert_eq!(p.keys(), vec![TEST_KEY.to_string()]);
    p.set_status(401);
    let ans = c.call("stream_url", json!({"device_id": p.host()})).await;
    assert_eq!(ans["error"]["code"], "stream_url_failed", "{ans}");
    let msg = ans["error"]["message"].as_str().unwrap();
    assert_eq!(
        msg,
        "POST /proxy/protect/integration/v1/cameras/cam1/rtsps-stream: HTTP 401"
    );
    assert!(!msg.contains(TEST_KEY) && !msg.contains("TOKEN-IN-ERROR-BODY"));
    let ans = c.call("stream_url", json!({"device_id": "nope"})).await;
    assert_eq!(ans["error"]["code"], "unknown_device", "{ans}");
    c.close().await;
}

// dial and answer are not_supported (the sentinel travels); open_relay finds no ffmpeg
// (slice 7b; slice 7a answered not_implemented) before it asks the console for a session, so
// no request reaches the console.
#[tokio::test]
async fn dial_answer_and_open_relay_refusals() {
    let (acceptor, _) = self_signed_acceptor();
    let p = FakeProtect::with_tls(Some(acceptor)).await;
    let mut c = start("refuse").await;
    c.raw("configure", cfg(&p.host(), json!({}))).await;
    let id = json!({"device_id": p.host(), "host": "192.168.2.1", "port": 5004});
    let ans = c.call("dial", id.clone()).await;
    assert_eq!(ans["error"]["code"], "not_supported", "{ans}");
    assert_eq!(
        ans["error"]["message"],
        "unifi_protect: no SIP dialog to dial (talkback is REST + RTP push): not_supported"
    );
    let ans = c.call("answer", id.clone()).await;
    assert_eq!(ans["error"]["code"], "not_supported", "{ans}");
    assert!(p.reqs().is_empty(), "{:?}", p.reqs());
    let ans = c.call("open_relay", id).await;
    assert_eq!(ans["error"]["code"], "open_relay_failed", "{ans}");
    assert_eq!(
        ans["error"]["message"],
        "unifi_protect: ffmpeg not found (installer prerequisite on HA Core, §9.9): exec: \"ffmpeg\": executable file not found in $PATH"
    );
    // the ffmpeg check runs BEFORE the console is asked for a session (PARITY.md 152)
    assert!(p.reqs().is_empty(), "{:?}", p.reqs());
    c.close().await;
}

// The configure refusals Python shows (Configure's own texts).
#[tokio::test]
async fn configure_refusals() {
    let mut c = start("cfgerr").await;
    let ans = c
        .call(
            "configure",
            json!({"driver": "unifi_protect", "config": {"nvr_host": "h"}}),
        )
        .await;
    assert_eq!(ans["error"]["code"], "configure_failed", "{ans}");
    assert_eq!(
        ans["error"]["message"],
        "unifi_protect: nvr_host, nvr_api_key and camera_id are required"
    );
    let ans = c.call("configure", cfg("h/x", json!({}))).await;
    assert_eq!(
        ans["error"]["message"], "unifi_protect: nvr_host must be a bare host or host:port",
        "{ans}"
    );
    let ans = c
        .call("configure", cfg("h", json!({"ring_events": "yes"})))
        .await;
    assert_eq!(
        ans["error"]["message"], "unifi_protect: ring_events must be true or false",
        "{ans}"
    );
    // unifi_protect_talk is ported since slice 7b: its own configure answers
    let ans = c
        .call(
            "configure",
            json!({"driver": "unifi_protect_talk", "config": {}}),
        )
        .await;
    assert_eq!(ans["error"]["code"], "configure_failed", "{ans}");
    assert_eq!(
        ans["error"]["message"],
        "unifi_protect_talk: device_id, protect_host, protect_api_key and camera_id are required"
    );
    c.close().await;
}

// A hub shutdown with a live ring and a connected watcher ends cleanly and closes the socket.
#[tokio::test]
async fn shutdown_with_a_live_watcher() {
    let f = FakeConsole::new(true).await;
    let mut c = start("shut").await;
    c.raw("configure", cfg(&f.host(), json!({"ring_events": true})))
        .await;
    f.last_conn().await;
    c.note("driver_availability", Duration::from_secs(5))
        .await
        .expect("available");
    f.send(&ring("s1")).await;
    c.note("ringing", Duration::from_secs(5))
        .await
        .expect("ringing");
    let t0 = tokio::time::Instant::now();
    c.hub.shutdown().await;
    assert!(t0.elapsed() < Duration::from_secs(5), "{:?}", t0.elapsed());
    let end = tokio::time::Instant::now() + Duration::from_secs(5);
    while !f.client_frames().iter().any(|(op, _)| *op == 8) {
        assert!(
            tokio::time::Instant::now() < end,
            "no close frame at shutdown"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    c.srv.close().await;
    let _ = std::fs::remove_dir_all(&c.dir);
}
