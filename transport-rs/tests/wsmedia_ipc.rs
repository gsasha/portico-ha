//! The away phone's audio over the signalling WebSocket joined to a device leg, over the real IPC
//! socket and with the real drivers (slice 8): `media_open` -> `wsMediaHub.join` ->
//! `joinCallToBrowser` for each way a driver holds a call: a ring dahua_sip waits to answer
//! (`findPendingRing`, the relay), a ring gate_sip_server answers in-process once the leg is up
//! (`findConnectAnswerer`), a call a driver answered itself (`findClaimedAudio`) and a
//! call out of `unifi_protect_talk` (`findClaimedAudio`). Before this slice `media_open` for such
//! a call answered `not_implemented` (PARITY.md 151, superseded by 156).
//!
//! No Go test exists for these joins (Go's `wsmedia_test.go` injects the join); the expectations
//! are the Go behaviour read from `main.go` `joinCallToBrowser` and checked on the wire. The tests
//! are serialised by one lock: they share the process-wide SIP-port seam of dahua_sip and the
//! fake `ffmpeg` on PATH.

mod common;

use std::sync::{Arc, OnceLock};
use std::time::Duration;

use common::dahua::{bye, invite_media, DStation};
use common::talk::*;
use common::unifi::self_signed_acceptor;
use common::unifi::FakeProtect;
use common::{find, has, Station};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use transport_rs::sfu::session::SessionConfig;
use transport_rs::sip::{parse_rtp_packet, parse_sdp, RtpPacket, RTP_FRAME_BYTES};
use transport_rs::util::{base64_decode, base64_encode};
use transport_rs::{dispatch, ipc};

static LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());
static SHIM: OnceLock<TmpDir> = OnceLock::new();

/// PATH is a directory with the fake ffmpeg (and the system's, for the script's `cat`/`sleep`).
fn shim() -> &'static TmpDir {
    SHIM.get_or_init(|| {
        let d = TmpDir::new("wsmediaipc");
        fake_ffmpeg_both(&d);
        std::env::set_var("PATH", format!("{}:/usr/bin:/bin", d.path().display()));
        d
    })
}

fn ms(n: u64) -> Duration {
    Duration::from_millis(n)
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

async fn start(tag: &str, host: &str) -> Client {
    shim();
    let dir = std::env::temp_dir().join(format!("wsmediaipc-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("t.sock");
    let srv = ipc::Server::listen(&sock).unwrap();
    let hub = dispatch::register(
        &srv,
        dispatch::HubOptions {
            sfu: SessionConfig::loopback(),
            our_host: host.to_string(),
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
    /// The next notification of `method` within `within` (the ones already read first).
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
    /// Like `note`, but the first notification that has `method` AND whose `call_id` and state
    /// match (state `""` = any).
    async fn note_state(&mut self, call_id: &str, state: &str, within: Duration) -> bool {
        let end = tokio::time::Instant::now() + within;
        loop {
            let left = end.saturating_duration_since(tokio::time::Instant::now());
            match self.note("call_state_changed", left).await {
                Some(n) if n["params"]["call_id"] == call_id => {
                    if state.is_empty() || n["params"]["state"] == state {
                        return true;
                    }
                }
                Some(_) => {}
                None => return false,
            }
        }
    }
    async fn close(self) {
        self.hub.shutdown().await;
        self.srv.close().await;
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn frame_b64(len: usize, seed: u8) -> String {
    base64_encode(
        &(0..len)
            .map(|i| seed.wrapping_add(i as u8))
            .collect::<Vec<_>>(),
    )
}

// ---- dahua_sip: the pending ring becomes a relay ---------------------------------------------------

/// A VTO rings; the phone opens a `pcmu` leg: nothing is answered before that, then the VTO gets its
/// 200 at once (a WebSocket leg is connected the moment it exists), the VTO's 16 kHz L16 reaches
/// Python as 320-byte `media_frame` notifications, Python's 320-byte frames reach the VTO as
/// 1280-byte RTP payloads, `media_close` notifies `media_closed` BEFORE its response and hangs the
/// VTO up, and the id can be opened again by a later call.
#[tokio::test]
async fn a_dahua_ring_is_answered_by_media_open_and_audio_flows_both_ways() {
    let _g = LOCK.lock().await;
    let st = DStation::new().await;
    let sip_port = common::dahua::SipPortGuard::set(st.port());
    let mut c = start("dahua", "127.0.0.1").await;
    let r = c.call("configure", json!({"driver": "dahua_sip", "config": {"host": "127.0.0.1", "sip_user": "8001#1", "sip_password": "pw"}})).await;
    drop(sip_port);
    assert!(r.get("result").is_some(), "{r}");
    assert!(st.wait_regs(ms(3000)).await);
    st.send(&invite_media("call-w1", "inv1", st.rtp_port()))
        .await;
    assert!(c.note("ringing", ms(2000)).await.is_some());
    assert!(st.wait_for("SIP/2.0 180", ms(2000)).await);
    st.collect(ms(10)).await;

    let r = c
        .call("media_open", json!({"call_id": "call-w1", "codec": "pcmu"}))
        .await;
    assert_eq!(
        r["result"],
        json!({"codec": "pcmu", "frame_bytes": 320, "frame_ms": 40}),
        "{r}"
    );
    assert!(
        st.wait_for("SIP/2.0 200", ms(3000)).await,
        "the VTO was not answered once the phone's leg was open"
    );
    let ok = st
        .collect(ms(50))
        .await
        .into_iter()
        .find(|m| m.starts_with("SIP/2.0 200"))
        .unwrap();
    let relay_port = parse_sdp(ok.split_once("\r\n\r\n").unwrap().1.as_bytes())
        .unwrap()
        .port as u16;

    // VTO -> phone: a 16 kHz L16 frame arrives as 320 bytes of mu-law
    let tone: Vec<u8> = (0..640i32)
        .flat_map(|i| (((i % 64) - 32) * 200).to_le_bytes()[..2].to_vec())
        .collect();
    assert_eq!(tone.len(), RTP_FRAME_BYTES);
    let pkt = RtpPacket {
        payload_type: 96,
        payload: tone,
        ..Default::default()
    }
    .marshal();
    st.rtp
        .send_to(&pkt, format!("127.0.0.1:{relay_port}"))
        .await
        .unwrap();
    let n = c
        .note("media_frame", ms(3000))
        .await
        .expect("Python heard nothing");
    assert_eq!(n["params"]["call_id"], "call-w1");
    assert_eq!(
        base64_decode(n["params"]["d"].as_str().unwrap())
            .unwrap()
            .len(),
        320
    );

    // phone -> VTO: 320 bytes of mu-law become one 1280-byte L16 payload
    let r = c
        .call(
            "media_frame",
            json!({"call_id": "call-w1", "d": frame_b64(320, 7)}),
        )
        .await;
    assert_eq!(r["result"], json!({"ok": true}), "{r}");
    let mut buf = vec![0u8; 4096];
    let (len, _) = tokio::time::timeout(ms(3000), st.rtp.recv_from(&mut buf))
        .await
        .expect("the VTO heard nothing")
        .unwrap();
    assert_eq!(
        parse_rtp_packet(&buf[..len]).unwrap().payload.len(),
        RTP_FRAME_BYTES
    );

    // media_close: media_closed first, then the response, then the BYE
    let line = c
        .raw(
            "media_close",
            json!({"call_id": "call-w1", "reason": "user"}),
        )
        .await;
    assert!(line.contains(r#""result":{"ok":true}"#), "{line}");
    let n = c
        .notes
        .iter()
        .position(|n| n["method"] == "media_closed")
        .map(|i| c.notes.remove(i))
        .expect("media_closed was not sent before the response");
    assert_eq!(n["params"], json!({"call_id": "call-w1", "reason": "user"}));
    assert!(
        st.wait_for("BYE ", ms(3000)).await,
        "the VTO was not hung up when the phone's leg closed"
    );
    // the id is free again
    let r = c
        .call("media_open", json!({"call_id": "call-w1", "codec": "l16"}))
        .await;
    assert_eq!(r["result"]["codec"], "l16", "{r}");
    c.close().await;
}

/// The VTO hangs up on its own: `ended` reaches Python, the relay is closed and forgotten (our BYE
/// goes out), the phone's leg stays Python's to close, and a frame from the phone is still accepted.
#[tokio::test]
async fn the_vto_hanging_up_ends_the_call_but_the_leg_stays_pythons_to_close() {
    let _g = LOCK.lock().await;
    let st = DStation::new().await;
    let sip_port = common::dahua::SipPortGuard::set(st.port());
    let mut c = start("dahua2", "127.0.0.1").await;
    c.call("configure", json!({"driver": "dahua_sip", "config": {"host": "127.0.0.1", "sip_user": "8001#1", "sip_password": "pw"}})).await;
    drop(sip_port);
    assert!(st.wait_regs(ms(3000)).await);
    st.send(&invite_media("call-w2", "inv2", st.rtp_port()))
        .await;
    assert!(c.note("ringing", ms(2000)).await.is_some());
    let r = c
        .call("media_open", json!({"call_id": "call-w2", "codec": "l16"}))
        .await;
    assert_eq!(r["result"]["frame_bytes"], 1280, "{r}");
    assert!(st.wait_for("SIP/2.0 200", ms(3000)).await);
    st.collect(ms(100)).await;
    st.send(&bye("call-w2", "bye2")).await;
    assert!(c.note_state("call-w2", "ended", ms(3000)).await);
    assert!(st.wait_for("BYE ", ms(3000)).await);
    let r = c
        .call(
            "media_frame",
            json!({"call_id": "call-w2", "d": frame_b64(1280, 1)}),
        )
        .await;
    assert_eq!(r["result"], json!({"ok": true}), "{r}");
    c.call("media_close", json!({"call_id": "call-w2"})).await;
    c.close().await;
}

/// A second leg for the same call id is refused (`already_open`); a leg for an id no driver holds
/// opens and carries nothing, as in Go; the VTO is not answered twice.
#[tokio::test]
async fn a_second_leg_and_a_leg_for_another_id_do_not_answer_the_vto_twice() {
    let _g = LOCK.lock().await;
    let st = DStation::new().await;
    let sip_port = common::dahua::SipPortGuard::set(st.port());
    let mut c = start("dahua3", "127.0.0.1").await;
    c.call("configure", json!({"driver": "dahua_sip", "config": {"host": "127.0.0.1", "sip_user": "8001#1", "sip_password": "pw"}})).await;
    drop(sip_port);
    assert!(st.wait_regs(ms(3000)).await);
    st.send(&invite_media("call-w3", "inv3", st.rtp_port()))
        .await;
    assert!(c.note("ringing", ms(2000)).await.is_some());
    let first = c
        .call("media_open", json!({"call_id": "call-w3", "codec": "l16"}))
        .await;
    assert!(first.get("result").is_some(), "{first}");
    assert!(st.wait_for("SIP/2.0 200", ms(3000)).await);
    // a second leg for the same call id is refused; one for another id joins nothing
    let r = c
        .call("media_open", json!({"call_id": "call-w3", "codec": "l16"}))
        .await;
    assert_eq!(r["error"]["code"], "already_open", "{r}");
    st.collect(ms(100)).await;
    let r = c
        .call("media_open", json!({"call_id": "other", "codec": "l16"}))
        .await;
    assert_eq!(r["result"]["codec"], "l16", "{r}");
    let got = st.collect(ms(400)).await;
    assert!(!got.iter().any(|m| m.starts_with("SIP/2.0 200")), "{got:?}");
    c.close().await;
}

// ---- gate_sip_server: a ring answered in-process once the leg is up -----------------------------------

fn free_udp() -> u16 {
    let s = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
    s.local_addr().unwrap().port()
}

/// The station rings; the phone's leg opens; the 200 goes out then and not before; the phone's frame
/// reaches the station; the phone's `media_close` hangs the call up.
#[tokio::test]
async fn a_gate_sip_server_ring_is_answered_in_process_by_media_open() {
    let _g = LOCK.lock().await;
    let mut c = start("gss", "").await;
    let (sip, rtp) = (free_udp(), free_udp());
    let cfg = json!({"driver":"gate_sip_server","config":{"device_id":"gate-w","sip_listen":format!("127.0.0.1:{sip}"),"rtp_port":rtp,"password":"s3cretpass"}});
    let r = c.call("configure", cfg).await;
    assert!(r.get("result").is_some(), "{r}");
    let st = Station::new(format!("127.0.0.1:{sip}").parse().unwrap()).await;
    st.register("").await;
    let m401 = find(&st.collect(ms(300)).await, "401").expect("no challenge");
    st.register(&common::digest_auth(&m401, "8001", "s3cretpass"))
        .await;
    assert!(find(&st.collect(ms(300)).await, "200").is_some());

    st.invite("call-w4", 1, &st.sdp("wide")).await;
    let ring = c.note("ringing", ms(2000)).await.expect("no ringing");
    assert_eq!(ring["params"]["call_id"], "call-w4");
    let early = st.collect(ms(300)).await;
    assert!(has(&early, "180") && !has(&early, "200"), "{early:?}");

    let r = c
        .call("media_open", json!({"call_id": "call-w4", "codec": "l16"}))
        .await;
    assert_eq!(r["result"]["frame_bytes"], 1280, "{r}");
    let got = st.collect(ms(1500)).await;
    assert!(
        has(&got, "200"),
        "no 200 once the phone's leg was open: {:?}",
        common::codes(&got)
    );
    st.ack("call-w4").await;
    assert!(c.note_state("call-w4", "", ms(2000)).await);

    // phone -> station: some RTP arrives once frames are sent
    for i in 0..10u8 {
        let r = c
            .call(
                "media_frame",
                json!({"call_id": "call-w4", "d": frame_b64(1280, i)}),
            )
            .await;
        assert_eq!(r["result"], json!({"ok": true}), "{r}");
    }
    let mut heard = 0;
    let end = tokio::time::Instant::now() + ms(2500);
    while tokio::time::Instant::now() < end && heard == 0 {
        if let Some((_, pl)) = st.read_rtp(ms(100)).await {
            heard += pl.len();
        }
    }
    assert!(heard > 0, "the station heard nothing from the phone");

    // the phone's leg closing hangs the station up
    c.call(
        "media_close",
        json!({"call_id": "call-w4", "reason": "user"}),
    )
    .await;
    assert!(
        has(&st.collect(ms(3000)).await, "BYE"),
        "the station was not hung up"
    );
    assert!(c.note_state("call-w4", "ended", ms(3000)).await);
    c.close().await;
}

/// DEV-811 P3: with `call_stats` in the configure and `stats: true` on the `media_open`, the frames leg's
/// counters ride `media_closed` and the call's gate-side audio counters arrive as a `call_audio_stats`
/// notification with the device id; the configure reply lists the feature. Nothing of it without the two
/// switches (the test above reads the unchanged `media_closed`).
#[tokio::test]
async fn call_stats_arrive_with_media_closed_and_as_call_audio_stats() {
    let _g = LOCK.lock().await;
    let mut c = start("gss-stats", "").await;
    let (sip, rtp) = (free_udp(), free_udp());
    let cfg = json!({"driver":"gate_sip_server","config":{"device_id":"gate-s","sip_listen":format!("127.0.0.1:{sip}"),"rtp_port":rtp,"password":"s3cretpass","call_stats":true}});
    let r = c.call("configure", cfg).await;
    assert_eq!(r["result"]["Features"], json!(["call_stats"]), "{r}");
    let st = Station::new(format!("127.0.0.1:{sip}").parse().unwrap()).await;
    st.register("").await;
    let m401 = find(&st.collect(ms(300)).await, "401").expect("no challenge");
    st.register(&common::digest_auth(&m401, "8001", "s3cretpass"))
        .await;
    assert!(find(&st.collect(ms(300)).await, "200").is_some());
    st.invite("call-s1", 1, &st.sdp("wide")).await;
    c.note("ringing", ms(2000)).await.expect("no ringing");
    st.collect(ms(300)).await;
    let r = c
        .call(
            "media_open",
            json!({"call_id": "call-s1", "codec": "l16", "stats": true}),
        )
        .await;
    assert_eq!(r["result"]["frame_bytes"], 1280, "{r}");
    st.collect(ms(1000)).await;
    st.ack("call-s1").await;
    assert!(c.note_state("call-s1", "", ms(2000)).await);
    // station -> phone: two 640-byte payloads make one 1280-byte frame; a loud one (peak 9000 clips at x4)
    let loud: Vec<u8> = (0..320).flat_map(|_| 9000i16.to_le_bytes()).collect();
    let rtp_addr: std::net::SocketAddr = format!("127.0.0.1:{rtp}").parse().unwrap();
    for i in 0..4u16 {
        st.rtp
            .send_to(&common::rtp_packet(96, i, u32::from(i) * 320, &loud), rtp_addr)
            .await
            .unwrap();
    }
    // phone -> station
    for i in 0..5u8 {
        let r = c
            .call(
                "media_frame",
                json!({"call_id": "call-s1", "d": frame_b64(1280, i)}),
            )
            .await;
        assert_eq!(r["result"], json!({"ok": true}), "{r}");
    }
    tokio::time::sleep(ms(300)).await;
    c.call(
        "media_close",
        json!({"call_id": "call-s1", "reason": "user"}),
    )
    .await;
    let closed = c.note("media_closed", ms(2000)).await.expect("no media_closed");
    let stats = &closed["params"]["stats"];
    assert_eq!(stats["uplink_frames"], 5, "{closed}");
    assert_eq!(stats["uplink_bad_frame"], 0);
    assert!(stats["downlink_frames"].as_u64().unwrap() >= 1, "{closed}");
    assert!(stats["uplink_gap_ms_max"].is_u64() && stats["uplink_gap_ms_p95"].is_u64());
    let cas = c
        .note("call_audio_stats", ms(4000))
        .await
        .expect("no call_audio_stats");
    let p = &cas["params"];
    assert_eq!((&p["device_id"], &p["call_id"]), (&json!("gate-s"), &json!("call-s1")));
    assert_eq!(p["station_rate"], 16000, "{cas}");
    assert!(p["station_frames"].as_u64().unwrap() >= 1, "{cas}");
    assert!(p["would_clip_frames"].as_u64().unwrap() >= 1, "{cas}");
    // the soft limiter bends what the x4 gain would have clipped (it no longer sits at full scale)
    assert!(p["down_limited_samples"].as_u64().unwrap() >= 1, "{cas}");
    assert_eq!(p["down_gain"].as_f64().unwrap(), 4.0, "{cas}");
    assert!(p["pre_gain_p50_dbfs"].is_i64() && p["up_gain"].is_number(), "{cas}");
    assert_eq!(p["pre_gain_hist"].as_array().unwrap().len(), 31);
    assert!(p["received_from_app"].as_u64().unwrap() >= 1, "{cas}");
    c.close().await;
}

/// The same call with neither switch: no `stats` in `media_closed`, no `call_audio_stats`, no `Features` change
/// beyond the listing (the counters stay off).
#[tokio::test]
async fn without_the_switches_there_are_no_stats_anywhere() {
    let _g = LOCK.lock().await;
    let mut c = start("gss-nostats", "").await;
    let (sip, rtp) = (free_udp(), free_udp());
    let cfg = json!({"driver":"gate_sip_server","config":{"device_id":"gate-n","sip_listen":format!("127.0.0.1:{sip}"),"rtp_port":rtp,"password":"s3cretpass"}});
    c.call("configure", cfg).await;
    let st = Station::new(format!("127.0.0.1:{sip}").parse().unwrap()).await;
    st.register("").await;
    let m401 = find(&st.collect(ms(300)).await, "401").expect("no challenge");
    st.register(&common::digest_auth(&m401, "8001", "s3cretpass"))
        .await;
    st.collect(ms(300)).await;
    st.invite("call-n1", 1, &st.sdp("wide")).await;
    c.note("ringing", ms(2000)).await.expect("no ringing");
    st.collect(ms(300)).await;
    c.call("media_open", json!({"call_id": "call-n1", "codec": "l16"})).await;
    st.collect(ms(1000)).await;
    st.ack("call-n1").await;
    assert!(c.note_state("call-n1", "", ms(2000)).await);
    c.call("media_close", json!({"call_id": "call-n1", "reason": "user"})).await;
    let closed = c.note("media_closed", ms(2000)).await.expect("no media_closed");
    assert!(closed["params"].get("stats").is_none(), "{closed}");
    assert_eq!(closed["params"], json!({"call_id": "call-n1", "reason": "user"}));
    assert!(c.note("call_audio_stats", ms(1500)).await.is_none());
    c.close().await;
}

// ---- unifi_protect_talk: a call out of the door ----------------------------------------------------------

/// `dial` placed a call out; the phone's leg takes its audio: the phone's frames reach the
/// uplink `ffmpeg`, the downlink `ffmpeg`'s PCM reaches the phone, and the leg closing hangs the
/// call up.
#[tokio::test]
async fn a_call_out_of_the_door_is_carried_by_media_open() {
    let _g = LOCK.lock().await;
    let sh = shim();
    let (acceptor, _) = self_signed_acceptor();
    let con = FakeProtect::with_tls(Some(acceptor)).await;
    let guard = FakeGuard::new(r#"{"sessionUp":false}"#).await;
    let mut c = start("talk", "").await;
    let cfg = json!({"driver": "unifi_protect_talk", "config": {
        "device_id": "ha_event:front", "protect_host": con.host(), "protect_api_key": common::unifi::TEST_KEY,
        "camera_id": "cam1", "guard_url": guard.url(), "downlink_url": "rtsp://127.0.0.1:1/doorbell"}});
    let ans = c.call("configure", cfg).await;
    assert_eq!(ans["result"]["CanDial"], true, "{ans}");
    let ans = c.call("dial", json!({"device_id": "ha_event:front"})).await;
    let call_id = ans["result"]["call_id"].as_str().unwrap().to_string();
    assert!(c.note_state(&call_id, "active", ms(5000)).await, "{ans}");

    let r = c
        .call("media_open", json!({"call_id": call_id, "codec": "l16"}))
        .await;
    assert_eq!(r["result"]["frame_bytes"], 1280, "{r}");
    // the phone's frames reach the uplink ffmpeg's stdin
    let sent = pcm(1280, 100);
    for _ in 0..6 {
        let r = c
            .call(
                "media_frame",
                json!({"call_id": call_id, "d": base64_encode(&sent)}),
            )
            .await;
        assert_eq!(r["result"], json!({"ok": true}), "{r}");
    }
    let end = tokio::time::Instant::now() + ms(5000);
    loop {
        let got = std::fs::read(sh.file("stdin")).unwrap_or_default();
        if got.windows(sent.len()).any(|w| w == sent.as_slice()) {
            break;
        }
        assert!(
            tokio::time::Instant::now() < end,
            "the phone's frame never reached ffmpeg ({} bytes there)",
            got.len()
        );
        tokio::time::sleep(ms(50)).await;
    }
    // the downlink ffmpeg starts producing PCM: it reaches the phone as media_frame
    sh.write("down.pcm", &pcm(1280 * 6, 5000));
    sh.write("go", b"");
    let n = c
        .note("media_frame", ms(8000))
        .await
        .expect("the door's audio never reached the phone");
    assert_eq!(n["params"]["call_id"], call_id.as_str());
    assert_eq!(
        base64_decode(n["params"]["d"].as_str().unwrap())
            .unwrap()
            .len(),
        1280
    );
    // closing the leg hangs the call up
    c.call("media_close", json!({"call_id": call_id})).await;
    assert!(c.note_state(&call_id, "ended", ms(5000)).await);
    let _ = std::fs::remove_file(sh.file("go"));
    c.close().await;
}

// ---- unifi_protect: a Protect ring answered through a talkback session ------------------------------

struct Quiet;
impl transport_rs::wsmedia::MediaNotifier for Quiet {
    fn notify(
        &self,
        _m: &str,
        _p: Value,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>> {
        Box::pin(async {})
    }
}

/// A doorbell ring of the Protect console: nothing is opened on the console before the phone's leg
/// exists; then the talkback session is opened and the phone's frames reach `ffmpeg`; closing the
/// leg hangs the ring call up and reaps `ffmpeg`. (The hub does not build this driver with a fake
/// console and `ffmpeg` it can address, so the join is driven through `WsMediaHub` directly with
/// the same `join_call_to_browser_with` the hub uses.)
#[tokio::test]
async fn a_protect_ring_is_answered_through_the_talkback_by_media_open() {
    use common::unifi::*;
    use transport_rs::cancel::CancelToken;
    use transport_rs::driver::unifiprotect::UnifiProtect;
    use transport_rs::driver::Driver;
    use transport_rs::webrtc::{join_call_to_browser_with, RelayRegistry};
    use transport_rs::wsmedia::{JoinFn, WsMediaHub};
    let _g = LOCK.lock().await;
    let f = FakeProtect::new().await;
    let con = FakeConsole::new(false).await;
    let dir = TmpDir::new("wsm-tb");
    let mut d = UnifiProtect::default();
    d.set_scheme("http");
    d.set_timing(fast());
    d.set_ffmpeg_binary(&fake_ffmpeg(&dir));
    d.configure(
        json!({"nvr_host": con.host(), "nvr_api_key": TEST_KEY, "camera_id": "cam1", "ring_events": true})
            .as_object()
            .unwrap(),
    )
    .unwrap();
    d.set_api_for_test(transport_rs::driver::unifiprotect::api::ProtectApi::new(
        "http",
        &f.host(),
        TEST_KEY,
        true,
    ));
    let d = Arc::new(d);
    let sink = RecSink::new();
    d.start(&CancelToken::new(), sink.clone()).await.unwrap();
    con.last_conn().await;
    con.send(r#"{"type":"add","item":{"id":"n1","modelKey":"event","type":"ring","start":1700000000000,"device":"cam1"}}"#).await;
    wait_for("ringing", || sink.has("ringing unifi-n1")).await;

    let drivers: Vec<Arc<dyn Driver>> = vec![d.clone()];
    let relays = RelayRegistry::new();
    let ctx = CancelToken::new();
    let (rl, cx) = (relays.clone(), ctx.clone());
    let join: JoinFn = Arc::new(move |id, leg| {
        join_call_to_browser_with(&cx, &drivers, &rl, "", id, leg);
    });
    let hub = WsMediaHub::new(Arc::new(Quiet), ms(60000), join);
    assert_eq!(
        f.reqs().len(),
        0,
        "the console was touched before the leg existed"
    );
    let params = |s: &str| -> ipc::Params {
        Some(serde_json::value::RawValue::from_string(s.to_string()).unwrap())
    };
    hub.open(params(r#"{"call_id":"unifi-n1","codec":"l16"}"#))
        .await
        .unwrap();
    wait_for("the talkback session", || {
        f.reqs().iter().any(|r| r.contains("talkback-session"))
    })
    .await;
    wait_for("the call to go active", || {
        sink.has("state unifi-n1 active")
    })
    .await;
    let frame = pcm(1280, 300);
    hub.frame(params(&format!(
        r#"{{"call_id":"unifi-n1","d":"{}"}}"#,
        base64_encode(&frame)
    )))
    .await
    .unwrap();
    wait_for("the phone's frame at ffmpeg", || dir.read("stdin") == frame).await;
    // closing the leg ends the call and reaps ffmpeg
    hub.close(params(r#"{"call_id":"unifi-n1"}"#))
        .await
        .unwrap();
    wait_for("the call to end", || sink.has("state unifi-n1 ended")).await;
    d.stop().await.unwrap();
}
