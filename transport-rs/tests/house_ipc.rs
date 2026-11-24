//! Ports of the 14 Go tests of `cmd/portico-transport/house_test.go` (the Go name in a comment on
//! each): the `house_open` / `house_close` verbs and the `media_frame` / `media_close` router over
//! a real IPC socket, with a real `WsMediaHub` for the door path (the part slice 8 left for this
//! slice), the real frame-channel legs and a real WebRTC session for the garbage-offer case; and
//! the whole binary, off against on.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use transport_rs::call::ClientLeg;
use transport_rs::house::{
    HouseHub, REASON_BRIDGE_CLOSED, REASON_HANGUP, REASON_SHUTDOWN, REASON_WEBRTC_FAILED,
    SESSION_PREFIX,
};
use transport_rs::sfu::session::SessionConfig;
use transport_rs::util::{base64_decode, base64_encode};
use transport_rs::wsmedia::{JoinFn, WsMediaHub, PCMU_FRAME_BYTES, WS_MEDIA_IDLE};
use transport_rs::{dispatch, ipc};

fn ms(n: u64) -> Duration {
    Duration::from_millis(n)
}

/// `rpc`: a blocking test client of the IPC socket; it keeps the notifications it reads while
/// waiting for a reply.
struct Rpc {
    rd: BufReader<tokio::net::unix::OwnedReadHalf>,
    wr: tokio::net::unix::OwnedWriteHalf,
    next: i64,
    notes: Vec<Value>,
    /// the `message` of the last error reply (empty after a result)
    last_message: String,
}

impl Rpc {
    async fn connect(sock: &std::path::Path) -> Rpc {
        let c = UnixStream::connect(sock).await.unwrap();
        let (rd, wr) = c.into_split();
        Rpc {
            rd: BufReader::new(rd),
            wr,
            next: 0,
            notes: Vec::new(),
            last_message: String::new(),
        }
    }

    async fn read_one(&mut self) -> Value {
        let mut l = String::new();
        let n = tokio::time::timeout(Duration::from_secs(3), self.rd.read_line(&mut l))
            .await
            .unwrap_or_else(|_| {
                panic!(
                    "reading from the hub timed out (notes so far: {:?})",
                    self.notes
                )
            })
            .unwrap();
        assert!(n > 0, "connection closed");
        serde_json::from_str(&l).expect("not JSON")
    }

    /// `call`: (result, error code); an error reply has no result.
    async fn call(&mut self, method: &str, params: Value) -> (Option<Value>, String) {
        self.next += 1;
        let id = self.next;
        let line = format!(
            "{}\n",
            json!({"id": id, "method": method, "params": params})
        );
        self.wr.write_all(line.as_bytes()).await.unwrap();
        loop {
            let m = self.read_one().await;
            if m.get("method").is_some() {
                self.notes.push(m);
                continue;
            }
            assert_eq!(m["id"].as_i64(), Some(id), "reply for another request: {m}");
            if let Some(e) = m.get("error").filter(|e| e.is_object()) {
                self.last_message = e["message"].as_str().unwrap_or_default().to_string();
                return (None, e["code"].as_str().unwrap().to_string());
            }
            self.last_message.clear();
            return (Some(m["result"].clone()), String::new());
        }
    }

    /// `waitNote`: the first notification (already read or still to come) that satisfies `pred`.
    async fn wait_note(&mut self, method: &str, pred: impl Fn(&Value) -> bool) -> Value {
        loop {
            if let Some(i) = self
                .notes
                .iter()
                .position(|n| n["method"] == method && pred(&n["params"]))
            {
                return self.notes.remove(i)["params"].clone();
            }
            let m = self.read_one().await;
            self.notes.push(m);
        }
    }

    /// `quiet`: true when no notification of `method` satisfying `pred` shows up within `d`.
    async fn quiet(&mut self, method: &str, d: Duration, pred: impl Fn(&Value) -> bool) -> bool {
        let end = tokio::time::Instant::now() + d;
        loop {
            let mut l = String::new();
            match tokio::time::timeout_at(end, self.rd.read_line(&mut l)).await {
                Ok(Ok(n)) if n > 0 => self.notes.push(serde_json::from_str(&l).unwrap()),
                _ => break,
            }
        }
        !self
            .notes
            .iter()
            .any(|n| n["method"] == method && pred(&n["params"]))
    }
}

fn hb64(n: usize, fill: u8) -> String {
    base64_encode(&vec![fill; n])
}

/// `houseRig`: a real ipc.Server with the door path's three media verbs (a real WsMediaHub) and
/// the house verbs.
struct Rig {
    c: Rpc,
    door: Arc<WsMediaHub>,
    hub: Option<Arc<HouseHub>>,
    joined: Arc<Mutex<Vec<String>>>,
    dir: std::path::PathBuf,
}

impl Drop for Rig {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

async fn rig(tag: &str, idle: Duration) -> Rig {
    let dir = std::env::temp_dir().join(format!("houseipc-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("t.sock");
    let srv = ipc::Server::listen(&sock).unwrap();
    let joined = Arc::new(Mutex::new(Vec::<String>::new()));
    let j2 = joined.clone();
    let join: JoinFn = Arc::new(move |id: &str, _leg: Arc<dyn ClientLeg>| {
        j2.lock().unwrap().push(id.to_string());
    });
    let door = WsMediaHub::new(Arc::new(srv.clone()), Duration::from_secs(60), join);
    for m in ["media_open", "media_frame", "media_close"] {
        let d = door.clone();
        srv.handle_inline(m, move |p: ipc::Params| {
            let d = d.clone();
            async move {
                match m {
                    "media_open" => d.open(p).await,
                    "media_frame" => d.frame(p).await,
                    _ => d.close(p).await,
                }
            }
        });
    }
    // (the off wiring is the dispatcher's: two not_supported answers, see the off tests)
    let hub = Some({
        let h = HouseHub::new(
            Arc::new(srv.clone()),
            door.clone(),
            idle,
            SessionConfig::loopback(),
        );
        h.register(&srv);
        h
    });
    let s = srv.clone();
    tokio::spawn(async move { s.serve().await });
    let c = Rpc::connect(&sock).await;
    Rig {
        c,
        door,
        hub,
        joined,
        dir,
    }
}

/// `openFramesCall`
async fn open_frames_call(c: &mut Rpc, id: &str, owners: &[&str]) -> Value {
    let o = |i: usize| owners.get(i).copied().unwrap_or("");
    let (res, code) = c
        .call(
            "house_open",
            json!({"call_id": id, "legs": [
                {"side": "from", "kind": "frames", "codec": "l16", "owner": o(0)},
                {"side": "to", "kind": "frames", "codec": "pcmu", "owner": o(1)},
            ]}),
        )
        .await;
    assert_eq!(code, "", "house_open {id}");
    res.unwrap()
}

/// `legIDs`
fn leg_ids(res: &Value) -> (String, String) {
    let (mut from, mut to) = (String::new(), String::new());
    for l in res["legs"].as_array().unwrap() {
        let id = l["leg_id"].as_str().unwrap().to_string();
        if l["side"] == "from" {
            from = id;
        } else {
            to = id;
        }
    }
    (from, to)
}

fn is_session_id(s: &str) -> bool {
    s.strip_prefix(SESSION_PREFIX).is_some_and(|h| {
        h.len() == 16
            && h.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}

// ---- off: the default -----------------------------------------------------------------------------

/// A dispatcher over a real socket (the production wiring), for the off tests and the cases that
/// need the whole verb table.
async fn dispatched(tag: &str, house_calls: bool) -> (Rpc, Arc<dispatch::Hub>, std::path::PathBuf) {
    let dir = std::env::temp_dir().join(format!("houseipc-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("t.sock");
    let srv = ipc::Server::listen(&sock).unwrap();
    let hub = dispatch::register(
        &srv,
        dispatch::HubOptions {
            house_calls,
            sfu: SessionConfig::loopback(),
            ..dispatch::HubOptions::default()
        },
    );
    let s = srv.clone();
    tokio::spawn(async move { s.serve().await });
    (Rpc::connect(&sock).await, hub, dir)
}

// TestHouseOffAnswersNotSupportedAndCreatesNothing
#[tokio::test]
async fn house_off_answers_not_supported_and_creates_nothing() {
    let (mut c, hub, dir) = dispatched("off1", false).await;
    assert!(
        !hub.has_house(),
        "a manager exists although the feature is off"
    );
    for v in ["house_open", "house_close"] {
        let (res, code) = c.call(v, json!({"call_id": "hso-1"})).await;
        assert!(
            res.is_none() && code == "not_supported",
            "{v}: {res:?} {code:?}"
        );
    }
    // the answer text too
    let line = {
        c.next += 1;
        let l = format!(
            "{}\n",
            json!({"id": c.next, "method": "house_open", "params": {}})
        );
        c.wr.write_all(l.as_bytes()).await.unwrap();
        c.read_one().await
    };
    assert_eq!(
        line["error"]["message"],
        "house calls are not enabled on this hub"
    );
    tokio::time::sleep(ms(50)).await; // the warm-up requests' tasks are done
    let rt = tokio::runtime::Handle::current();
    let before = rt.metrics().num_alive_tasks();
    // even a well-formed open creates no bridge, no leg, no task
    for i in 0..5 {
        let (_, code) = c
            .call(
                "house_open",
                json!({"call_id": format!("hso-{i}"), "legs": [
                    {"side": "from", "kind": "frames", "codec": "l16"},
                    {"side": "to", "kind": "frames", "codec": "l16"},
                ]}),
            )
            .await;
        assert_eq!(code, "not_supported", "well-formed open while off");
    }
    tokio::time::sleep(ms(50)).await;
    let after = rt.metrics().num_alive_tasks();
    assert!(after <= before, "tasks {before} -> {after}");
    assert!(
        hub.door_leg_ids().is_empty(),
        "the door table changed: {:?}",
        hub.door_leg_ids()
    );
    hub.shutdown().await; // the off stop is a no-op
    let _ = std::fs::remove_dir_all(dir);
}

// TestHouseOffLeavesTheDoorMediaVerbsAsTheyWere
#[tokio::test]
async fn house_off_leaves_the_door_media_verbs_as_they_were() {
    let (mut c, hub, dir) = dispatched("off2", false).await;
    // a house-looking id is just an unknown id to the door path while the feature is off
    for id in ["house-abc-from", "gso-unknown", "nothing"] {
        let (_, code) = c
            .call("media_frame", json!({"call_id": id, "d": hb64(1280, 1)}))
            .await;
        assert_eq!(code, "unknown_session", "media_frame {id}");
        let (res, code) = c.call("media_close", json!({"call_id": id})).await;
        assert!(
            code.is_empty() && res.unwrap()["ok"] == true,
            "media_close {id}"
        );
    }
    let (res, code) = c
        .call("media_open", json!({"call_id": "gso-1", "codec": "pcmu"}))
        .await;
    let res = res.unwrap();
    assert!(
        code.is_empty() && res["codec"] == "pcmu" && res["frame_bytes"] == PCMU_FRAME_BYTES,
        "media_open: {res} {code:?}"
    );
    let (_, code) = c
        .call(
            "media_frame",
            json!({"call_id": "gso-1", "d": hb64(PCMU_FRAME_BYTES, 1)}),
        )
        .await;
    assert_eq!(code, "", "door frame");
    hub.shutdown().await;
    let _ = std::fs::remove_dir_all(dir);
}

// ---- on -------------------------------------------------------------------------------------------

// TestHouseOnOpensABridgeAndCarriesAudioBothWays
#[tokio::test]
async fn house_on_opens_a_bridge_and_carries_audio_both_ways() {
    let mut r = rig("on1", Duration::from_secs(60)).await;
    let res = open_frames_call(&mut r.c, "hso-1", &[]).await;
    assert!(
        is_session_id(res["session"].as_str().unwrap()),
        "session {}",
        res["session"]
    );
    let (from, to) = leg_ids(&res);
    for m in res["legs"].as_array().unwrap() {
        let want = if m["side"] == "from" { 1280 } else { 320 };
        assert!(
            m["state"] == "attached" && m["frame_bytes"] == want && m["frame_ms"] == 40,
            "leg {m}"
        );
    }
    // the from-phone's l16 frame goes up as media_frame and comes out for the to-phone as 320 bytes of pcmu
    let (_, code) =
        r.c.call("media_frame", json!({"call_id": from, "d": hb64(1280, 0)}))
            .await;
    assert_eq!(code, "", "uplink");
    let t = to.clone();
    let n =
        r.c.wait_note("media_frame", move |p| p["call_id"] == t.as_str())
            .await;
    assert_eq!(
        base64_decode(n["d"].as_str().unwrap()).unwrap().len(),
        PCMU_FRAME_BYTES
    );
    // and the other way, pcmu in, l16 out
    let (_, code) =
        r.c.call("media_frame", json!({"call_id": to, "d": hb64(320, 0xFF)}))
            .await;
    assert_eq!(code, "", "reverse uplink");
    let f = from.clone();
    let n =
        r.c.wait_note("media_frame", move |p| p["call_id"] == f.as_str())
            .await;
    assert_eq!(base64_decode(n["d"].as_str().unwrap()).unwrap().len(), 1280);
    r.c.wait_note("house_leg_state", |p| {
        p["state"] == "first_frame" && p["call_id"] == "hso-1"
    })
    .await;
    // a wrong-size frame is the door path's bad_frame
    let (_, code) =
        r.c.call("media_frame", json!({"call_id": from, "d": hb64(7, 1)}))
            .await;
    assert_eq!(code, "bad_frame", "short frame");
    let (_, code) =
        r.c.call("media_frame", json!({"call_id": from, "d": "***"}))
            .await;
    assert_eq!(code, "bad_params", "not base64");
    let (_, code) =
        r.c.call(
            "media_frame",
            json!({"call_id": "house-0000000000000000-from", "d": hb64(1280, 0)}),
        )
        .await;
    assert_eq!(code, "unknown_session", "unknown house leg");
}

// TestHouseCloseEndsBothLegsAndIsIdempotent
#[tokio::test]
async fn house_close_ends_both_legs_and_is_idempotent() {
    let mut r = rig("close", Duration::from_secs(60)).await;
    let (from, to) = leg_ids(&open_frames_call(&mut r.c, "hso-1", &[]).await);
    let (res, code) = r.c.call("house_close", json!({"call_id": "hso-1"})).await;
    let res = res.unwrap();
    assert!(
        code.is_empty() && res["ok"] == true && res["closed"] == true,
        "close: {res} {code:?}"
    );
    for id in [from, to] {
        let got =
            r.c.wait_note("media_closed", move |p| p["call_id"] == id.as_str())
                .await;
        assert_eq!(got["reason"], REASON_HANGUP, "media_closed {got}");
    }
    let (res, code) = r.c.call("house_close", json!({"call_id": "hso-1"})).await;
    assert!(
        code.is_empty() && res.unwrap()["closed"] == false,
        "second close"
    );
    let (_, code) = r.c.call("house_close", json!({})).await;
    assert_eq!(code, "bad_params", "close without an id");
    assert_eq!(
        r.hub.as_ref().unwrap().manager().count(),
        0,
        "bridge still counted"
    );
    // a requested close does not echo house_leg_state
    assert!(
        r.c.quiet("house_leg_state", ms(50), |p| p["state"] == "lost")
            .await,
        "house_close produced a lost event"
    );
}

// TestHouseAPhonesGoodbyeEndsTheCallAndTheOtherPhoneIsTold
#[tokio::test]
async fn house_a_phones_goodbye_ends_the_call_and_the_other_phone_is_told() {
    let mut r = rig("bye", Duration::from_secs(60)).await;
    let (from, to) = leg_ids(&open_frames_call(&mut r.c, "hso-1", &[]).await);
    let (_, code) =
        r.c.call(
            "media_close",
            json!({"call_id": from, "reason": "phone_hung_up"}),
        )
        .await;
    assert_eq!(code, "");
    r.c.wait_note("media_closed", move |p| p["call_id"] == to.as_str())
        .await;
    let from_lost =
        r.c.wait_note("house_leg_state", |p| {
            p["state"] == "lost" && p["leg"] == "from"
        })
        .await;
    let to_lost =
        r.c.wait_note("house_leg_state", |p| {
            p["state"] == "lost" && p["leg"] == "to"
        })
        .await;
    assert!(
        from_lost["reason"] == "phone_hung_up"
            && to_lost["reason"] == REASON_BRIDGE_CLOSED
            && from_lost["call_id"] == "hso-1",
        "lost events {from_lost} {to_lost}"
    );
}

// R2-order (PARITY 360): Go closes the two legs one after the other in the handler's own goroutine
// (`end` calls `leg.Close` for from, then to; each runs its callback, which `Notify`s), so BOTH
// `media_closed` are on the wire before the reply of the phone's `media_close`, from first. Only the
// `lost` events (a `go func()` in `end`) race the reply. The client reads the wire in order: what it
// had read when the reply came is exactly what was written before it.
// Mutation: the closes are not awaited by the handler / the legs are closed to first.
#[tokio::test]
async fn house_a_phones_goodbye_writes_both_media_closed_before_the_reply_from_first() {
    let mut r = rig("byeorder", Duration::from_secs(60)).await;
    let (from, to) = leg_ids(&open_frames_call(&mut r.c, "hso-1", &[]).await);
    let (_, code) =
        r.c.call(
            "media_close",
            json!({"call_id": from, "reason": "phone_hung_up"}),
        )
        .await;
    assert_eq!(code, "");
    let before_reply: Vec<(String, String)> =
        r.c.notes
            .iter()
            .filter(|n| n["method"] == "media_closed")
            .map(|n| {
                (
                    n["params"]["call_id"].as_str().unwrap().to_string(),
                    n["params"]["reason"].as_str().unwrap().to_string(),
                )
            })
            .collect();
    assert_eq!(
        before_reply,
        [
            (from, "phone_hung_up".to_string()),
            (to, "phone_hung_up".to_string())
        ],
        "media_closed lines read before the reply"
    );
}

// TestHouseUplinkIdleRuleKeepsTheHubsFiveSecondsShape
#[tokio::test]
async fn house_uplink_idle_rule_keeps_the_hubs_five_seconds_shape() {
    // the factory is given the door path's own constant, not a number of its own
    assert_eq!(
        WS_MEDIA_IDLE,
        Duration::from_secs(5),
        "the hub's uplink idle is no longer 5 s: re-check the house rule"
    );
    let src = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/src/dispatch/mod.rs"))
        .unwrap();
    let wiring = &src[src
        .find("crate::house::HouseHub::new(")
        .expect("the wiring")..];
    assert!(
        wiring[..wiring.find(");").unwrap()].contains("WS_MEDIA_IDLE"),
        "dispatch/mod.rs must hand the house hub WS_MEDIA_IDLE"
    );
    // and with a short idle a frames leg that stops sending ends the call
    let mut r = rig("idle", ms(120)).await;
    open_frames_call(&mut r.c, "hso-1", &[]).await;
    let ev =
        r.c.wait_note("house_leg_state", |p| p["state"] == "lost")
            .await;
    assert_eq!(ev["call_id"], "hso-1", "event {ev}");
    assert_eq!(
        r.hub.as_ref().unwrap().manager().count(),
        0,
        "an idle call is still counted"
    );
}

// TestHouseRefusals
#[tokio::test]
async fn house_refusals() {
    let mut r = rig("refuse", Duration::from_secs(60)).await;
    let legs = json!([
        {"side": "from", "kind": "frames", "codec": "l16"},
        {"side": "to", "kind": "frames", "codec": "l16"}
    ]);
    let cases = [
        ("no id", json!({"legs": legs})),
        ("a door's id", json!({"call_id": "gso-1", "legs": legs})),
        (
            "one leg",
            json!({"call_id": "hso-1", "legs": [legs[0].clone()]}),
        ),
        ("no legs", json!({"call_id": "hso-1"})),
        (
            "bad codec",
            json!({"call_id": "hso-1", "legs": [legs[0].clone(), {"side": "to", "kind": "frames", "codec": "mp3"}]}),
        ),
        (
            "webrtc no sdp",
            json!({"call_id": "hso-1", "legs": [legs[0].clone(), {"side": "to", "kind": "webrtc"}]}),
        ),
    ];
    for (name, p) in cases {
        let (res, code) = r.c.call("house_open", p).await;
        assert!(
            res.is_none() && code == "bad_params",
            "{name}: {res:?} {code:?}"
        );
    }
    assert_eq!(
        r.hub.as_ref().unwrap().manager().count(),
        0,
        "a refused open took a slot"
    );
    open_frames_call(&mut r.c, "hso-1", &[]).await;
    let (_, code) =
        r.c.call("house_open", json!({"call_id": "hso-1", "legs": legs}))
            .await;
    assert_eq!(code, "already_open", "duplicate");
    let (_, code) =
        r.c.call(
            "house_open",
            json!({"call_id": "hso-2", "legs": [
                {"side": "from", "kind": "frames", "codec": "l16", "owner": "alice"},
                {"side": "to", "kind": "frames", "codec": "l16", "owner": "alice"}]}),
        )
        .await;
    assert_eq!(code, "", "alice's call");
    let (_, code) =
        r.c.call(
            "house_open",
            json!({"call_id": "hso-3", "legs": [
                {"side": "from", "kind": "frames", "codec": "l16", "owner": "alice"},
                {"side": "to", "kind": "frames", "codec": "l16"}]}),
        )
        .await;
    assert_eq!(code, "too_many_calls", "alice's third leg");
}

// TestHouseCapOfFourAndADoorIsNeverOutrankedByIt
#[tokio::test]
async fn house_cap_of_four_and_a_door_is_never_outranked_by_it() {
    let mut r = rig("cap", Duration::from_secs(60)).await;
    for i in 1..=4 {
        open_frames_call(&mut r.c, &format!("hso-{i}"), &[]).await;
    }
    let legs = json!([
        {"side": "from", "kind": "frames", "codec": "l16"},
        {"side": "to", "kind": "frames", "codec": "l16"}
    ]);
    let (_, code) =
        r.c.call("house_open", json!({"call_id": "hso-5", "legs": legs}))
            .await;
    assert_eq!(code, "too_many_calls", "fifth");
    // With every house slot taken a door still gets its media leg: separate tables, no shared cap.
    for door in ["gso-a", "gso-b", "gso-c"] {
        let (_, code) =
            r.c.call("media_open", json!({"call_id": door, "codec": "pcmu"}))
                .await;
        assert_eq!(code, "", "door {door} refused while house calls are full");
    }
    // and the two tables stay apart
    assert_eq!(r.door.len(), 3);
    assert_eq!(r.hub.as_ref().unwrap().manager().count(), 4);
    // the REAL door table (Go iterates `hub.legs`, house_test.go:400-404; R2-77)
    let ids = r.door.ids();
    assert_eq!(ids.len(), 3);
    for id in &ids {
        assert!(r.door.leg(id).is_some());
        assert!(
            !id.starts_with(SESSION_PREFIX),
            "a door id looks like a house id: {id}"
        );
    }
    assert_eq!(r.joined.lock().unwrap().len(), 3);
}

// TestHouseOnLeavesDoorFramesToTheDoorPath
#[tokio::test]
async fn house_on_leaves_door_frames_to_the_door_path() {
    let mut r = rig("door", Duration::from_secs(60)).await;
    let (_, code) =
        r.c.call("media_open", json!({"call_id": "gso-1", "codec": "pcmu"}))
            .await;
    assert_eq!(code, "");
    let (_, code) =
        r.c.call(
            "media_frame",
            json!({"call_id": "gso-1", "d": hb64(PCMU_FRAME_BYTES, 1)}),
        )
        .await;
    assert_eq!(code, "", "door frame");
    let leg = r.door.leg("gso-1").expect("the door leg");
    let out = ClientLeg::output(&*leg);
    let got = tokio::time::timeout(Duration::from_secs(1), async {
        out.lock().await.recv().await
    })
    .await;
    assert!(
        matches!(got, Ok(Some(_))),
        "the door's frame did not reach the door leg"
    );
    let (_, code) =
        r.c.call(
            "media_frame",
            json!({"call_id": "gso-other", "d": hb64(320, 1)}),
        )
        .await;
    assert_eq!(code, "unknown_session", "unknown door id");
    // a door's close is the door's close: the leg goes, the house table is untouched
    open_frames_call(&mut r.c, "hso-1", &[]).await;
    let (_, code) = r.c.call("media_close", json!({"call_id": "gso-1"})).await;
    assert_eq!(code, "");
    r.c.wait_note("media_closed", |p| p["call_id"] == "gso-1")
        .await;
    assert_eq!(
        r.hub.as_ref().unwrap().manager().count(),
        1,
        "a door's close touched the house call"
    );
}

// DEV-796 C6: a house frame's `d` over the cap is refused before it is decoded (invalid base64 of
// that size used to come back as bad_params).
#[tokio::test]
async fn house_an_overlong_frame_is_refused_before_decoding() {
    let mut r = rig("overlong", Duration::from_secs(60)).await;
    let res = open_frames_call(&mut r.c, "hso-c6", &[]).await;
    let (from, _) = leg_ids(&res);
    let (_, code) =
        r.c.call(
            "media_frame",
            json!({"call_id": from, "d": "=".repeat(1 << 19)}),
        )
        .await;
    assert_eq!(code, "bad_frame");
}

// TestHouseShutdownEndsEveryBridge
#[tokio::test]
async fn house_shutdown_ends_every_bridge() {
    let mut r = rig("stop", Duration::from_secs(60)).await;
    let (from1, to1) = leg_ids(&open_frames_call(&mut r.c, "hso-1", &[]).await);
    let (from2, to2) = leg_ids(&open_frames_call(&mut r.c, "hso-2", &[]).await);
    r.hub.as_ref().unwrap().stop().await;
    for id in [from1, to1, from2, to2] {
        let id2 = id.clone();
        let got =
            r.c.wait_note("media_closed", move |p| p["call_id"] == id2.as_str())
                .await;
        assert_eq!(got["reason"], REASON_SHUTDOWN, "leg {id} closed with {got}");
    }
    let (_, code) =
        r.c.call(
            "house_open",
            json!({"call_id": "hso-9", "legs": [
                {"side": "from", "kind": "frames", "codec": "l16"},
                {"side": "to", "kind": "frames", "codec": "l16"}]}),
        )
        .await;
    assert_eq!(code, "shutting_down", "open after shutdown");
    // the text too (R2-40): Go's main.go answers `transport is stopping`
    assert_eq!(r.c.last_message, "transport is stopping");
}

// TestHouseAWebRTCLegAnswersByEventAndOpenDoesNotWaitForIt
#[tokio::test]
async fn house_a_webrtc_leg_answers_by_event_and_open_does_not_wait_for_it() {
    let mut r = rig("webrtc", Duration::from_secs(60)).await;
    // a garbage offer: the real session refuses it; the failure arrives as events, and the open reply came first
    let (res, code) =
        r.c.call(
            "house_open",
            json!({"call_id": "hso-1", "legs": [
                {"side": "from", "kind": "webrtc", "sdp": "not an sdp"},
                {"side": "to", "kind": "frames", "codec": "l16"}]}),
        )
        .await;
    assert_eq!(code, "", "open");
    for m in res.unwrap()["legs"].as_array().unwrap() {
        if m["side"] == "from" {
            assert!(
                m["state"] == "pending" && m.get("leg_id").is_none(),
                "webrtc leg {m}"
            );
        }
    }
    let lost =
        r.c.wait_note("house_leg_state", |p| {
            p["state"] == "lost" && p["leg"] == "from"
        })
        .await;
    assert_eq!(lost["reason"], REASON_WEBRTC_FAILED, "lost {lost}");
}

// ---- the whole binary: verb table, replies and file descriptors, off against on --------------------

const DOOR_VERBS: [&str; 14] = [
    "configure",
    "deconfigure",
    "dial",
    "answer",
    "open_relay",
    "stream_url",
    "hangup",
    "keepalive",
    "webrtc_offer",
    "webrtc_answer",
    "webrtc_ice",
    "media_open",
    "media_frame",
    "media_close",
];

/// `goldenDoorReplies`: what the hub answers each existing verb with empty params. It is the verb
/// table as it stood before house calls; the feature must not move a single entry, off or on.
fn golden_door_replies() -> Vec<(&'static str, &'static str)> {
    vec![
        ("configure", "unknown_driver"),
        ("deconfigure", "unknown_device"),
        ("dial", "unknown_device"),
        ("answer", "unknown_device"),
        ("open_relay", "unknown_device"),
        ("stream_url", "unknown_device"),
        ("hangup", "not_implemented"),
        ("keepalive", "not_implemented"),
        ("webrtc_offer", "bad_params"),
        ("webrtc_answer", "unknown_session"),
        ("webrtc_ice", "unknown_session"),
        ("media_open", "bad_params"),
        ("media_frame", "unknown_session"),
        ("media_close", ""), // ok
        ("__nope__", "unknown_method"),
    ]
}

struct ProcGuard(std::process::Child, std::path::PathBuf);

impl Drop for ProcGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
        let _ = std::fs::remove_dir_all(&self.1);
    }
}

struct Child {
    _p: ProcGuard,
    c: Rpc,
    fds: usize,
}

/// `startChild`
async fn start_child(tag: &str, extra: &[&str]) -> Child {
    let dir = std::env::temp_dir().join(format!("houseipc-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("h.sock");
    let proc = std::process::Command::new(env!("CARGO_BIN_EXE_portico-transport"))
        .arg("-socket")
        .arg(&sock)
        .args(extra)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .expect("the binary starts");
    let pid = proc.id();
    let guard = ProcGuard(proc, dir);
    let mut conn = None;
    for _ in 0..200 {
        if let Ok(c) = UnixStream::connect(&sock).await {
            conn = Some(c);
            break;
        }
        tokio::time::sleep(ms(25)).await;
    }
    let conn = conn.expect("hub did not come up");
    let (rd, wr) = conn.into_split();
    let mut c = Rpc {
        rd: BufReader::new(rd),
        wr,
        next: 0,
        notes: Vec::new(),
        last_message: String::new(),
    };
    // one reply first: the accept loop is certainly past setup when the fds are counted
    c.call("__nope__", json!({})).await;
    let fds = std::fs::read_dir(format!("/proc/{pid}/fd"))
        .map(|e| e.count())
        .unwrap_or(0);
    Child { _p: guard, c, fds }
}

async fn reply_table(h: &mut Child) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for v in DOOR_VERBS.iter().copied().chain(["__nope__"]) {
        let (_, code) = h.c.call(v, json!({})).await;
        out.push((v.to_string(), code));
    }
    out
}

// TestBinaryVerbTableAndRepliesAreIdenticalOffAndOn
#[tokio::test]
async fn binary_verb_table_and_replies_are_identical_off_and_on() {
    let mut off = start_child("boff", &[]).await;
    let mut on = start_child("bon", &["-house-calls"]).await;
    let (off_t, on_t) = (reply_table(&mut off).await, reply_table(&mut on).await);
    for (v, want) in golden_door_replies() {
        let get = |t: &Vec<(String, String)>| t.iter().find(|(k, _)| k == v).unwrap().1.clone();
        assert_eq!(get(&off_t), want, "off: {v}");
        assert_eq!(get(&on_t), want, "on: {v}");
    }
    // the house verbs: not_supported off, handled on
    for v in ["house_open", "house_close"] {
        let (_, code) = off.c.call(v, json!({})).await;
        assert_eq!(code, "not_supported", "off: {v}");
        let (_, code) = on.c.call(v, json!({})).await;
        assert_eq!(code, "bad_params", "on: {v}");
    }
    // no new listener or socket: the same number of descriptors with the feature on and off
    if off.fds != 0 {
        assert_eq!(
            off.fds, on.fds,
            "descriptors: off {}, on {}",
            off.fds, on.fds
        );
    }
}

// TestBinaryOffAndUnflaggedAreTheSame
#[tokio::test]
async fn binary_off_and_unflagged_are_the_same() {
    let mut plain = start_child("bplain", &[]).await;
    let mut explicit = start_child("bexpl", &["-house-calls=false"]).await;
    assert_eq!(
        reply_table(&mut plain).await,
        reply_table(&mut explicit).await
    );
    for h in [&mut plain, &mut explicit] {
        let (_, code) = h.c.call("house_open", json!({"call_id": "hso-1"})).await;
        assert_eq!(code, "not_supported");
    }
}

// ---- no existing code path reaches the bridge ------------------------------------------------------

fn read_src(rel: &str) -> String {
    std::fs::read_to_string(format!("{}/src/{rel}", env!("CARGO_MANIFEST_DIR"))).unwrap()
}

fn rs_files(dir: &std::path::Path, out: &mut Vec<std::path::PathBuf>) {
    for e in std::fs::read_dir(dir).unwrap() {
        let p = e.unwrap().path();
        if p.is_dir() {
            rs_files(&p, out);
        } else if p.extension().is_some_and(|x| x == "rs") {
            out.push(p);
        }
    }
}

// TestNoExistingCodeReferencesTheHouseBridge
#[test]
fn no_existing_code_references_the_house_bridge() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut files = Vec::new();
    rs_files(&root, &mut files);
    let ours = [
        "crate::house",
        "HouseHub",
        "HubFactory",
        "house_open",
        "house_close",
        "house_leg_state",
        "house::SESSION_PREFIX",
        "transport_rs::house",
    ];
    // R2-9: the files that may name the bridge are house/ and dispatch/mod.rs (its test module is
    // checked below); `dispatch/tests/` is test code. main.rs, process/flags.rs and lib.rs carry the
    // flag only: every line of them is checked against an allow-list (Go checks every line of
    // main.go and forbids the import), as is every other file against `ours`.
    let flag_files = ["main.rs", "process/flags.rs", "lib.rs"];
    let flag_allowed = [
        "house_calls",
        "house-calls",
        "house_open / house_close",
        "pub mod house;",
    ];
    for f in &files {
        let rel = f.strip_prefix(&root).unwrap().to_string_lossy().to_string();
        if rel.starts_with("house/")
            || rel == "dispatch/mod.rs"
            || rel.starts_with("dispatch/tests/")
        {
            continue;
        }
        let text = std::fs::read_to_string(f).unwrap();
        for line in text.lines() {
            let code = line.trim_start();
            if code.starts_with("//") {
                continue;
            }
            if flag_files.contains(&rel.as_str()) {
                if code.to_lowercase().contains("house") {
                    assert!(
                        flag_allowed.iter().any(|a| code.contains(a)),
                        "{rel} mentions house outside the flag: {line}"
                    );
                }
                for n in [
                    "crate::house",
                    "HouseHub",
                    "HubFactory",
                    "house_leg_state",
                    "house::SESSION_PREFIX",
                    "transport_rs::house",
                ] {
                    assert!(!code.contains(n), "{rel} names the bridge ({n}): {line}");
                }
                continue;
            }
            for n in ours {
                assert!(!code.contains(n), "{rel} names the bridge ({n}): {line}");
            }
        }
    }
    // dispatch/mod.rs: one registration, one stop, and nothing else of the bridge; every other line
    // that mentions house is the flag, the off answer or a comment
    let d = read_src("dispatch/mod.rs");
    let prod = &d[..d.find("#[cfg(test)]\nmod tests").expect("the test module")];
    assert_eq!(
        prod.matches("HouseHub::new(").count(),
        1,
        "dispatch/mod.rs builds the house hub once"
    );
    assert_eq!(
        prod.matches("h.stop().await").count(),
        1,
        "dispatch/mod.rs stops the house hub once"
    );
    let allowed = [
        "house_calls",
        "house_hub",
        "house_sfu",
        "sfu_for_house",
        "HouseHub",
        "house_open",
        "house_close",
        "HOUSE_NOT_ENABLED_MESSAGE",
        "house:",
        "self.house",
        "= house",
        "house_hub.register",
        "has_house",
        "let house =",
        "= house {",
    ];
    for (i, line) in prod.lines().enumerate() {
        let t = line.trim_start();
        if t.starts_with("//") || t.starts_with("///") {
            continue;
        }
        if t.to_lowercase().contains("house") {
            assert!(
                allowed.iter().any(|a| t.contains(a)),
                "dispatch/mod.rs:{} mentions house outside the seam: {line}",
                i + 1
            );
        }
    }
    // the hub's shutdown ends the bridges right after cancel() and before the sessions and the drivers stop
    let sd = &prod[prod.find("pub async fn shutdown").unwrap()..];
    let (i_cancel, i_house, i_sessions, i_drivers) = (
        sd.find("self.ctx.cancel();").unwrap(),
        sd.find("h.stop().await").unwrap(),
        sd.find("self.sessions.close_all_wait").unwrap(),
        sd.find("let all: Vec<Arc<dyn Driver>>").unwrap(),
    );
    assert!(
        i_cancel < i_house && i_house < i_sessions && i_sessions < i_drivers,
        "shutdown order"
    );
    // the bridge core (everything above the hub glue) imports none of the door paths or the
    // transport glue (imports, not comments)
    // (house/mod.rs, manager.rs and bridge.rs; hub.rs and params.rs are the hub's side)
    let core = ["house/mod.rs", "house/manager.rs", "house/bridge.rs"]
        .map(read_src)
        .join("\n");
    // and no other file of the module is allowed to name the door path or the transports
    for f in [
        "house/mod.rs",
        "house/manager.rs",
        "house/bridge.rs",
        "house/params.rs",
    ] {
        for line in read_src(f).lines() {
            let t = line.trim_start();
            assert!(
                t.starts_with("//")
                    || !(t.contains("WsMediaHub") || t.contains("crate::sfu::session")),
                "{f} names a transport: only house/hub.rs may: {line}"
            );
        }
    }
    for line in core.lines() {
        let t = line.trim_start();
        if t.starts_with("//") {
            continue;
        }
        for bad in [
            "crate::sfu",
            "crate::call",
            "crate::dispatch",
            "crate::webrtc",
            "crate::driver::dahua",
            "crate::driver::unifiprotect",
            "crate::driver::gatesipserver",
            "crate::wsmedia",
            "crate::ipc",
            "WsMediaHub",
            "ClientLeg",
        ] {
            assert!(!t.contains(bad), "the bridge core mentions {bad}: {line}");
        }
    }
    // ipc/: documentation comments only (every file of the module, tcp.rs included)
    let ipc_dir = root.join("ipc");
    let mut ipc_files = Vec::new();
    rs_files(&ipc_dir, &mut ipc_files);
    assert!(ipc_files.len() >= 3, "ipc/*.rs not found: {ipc_files:?}");
    for f in ipc_files {
        for (i, line) in std::fs::read_to_string(&f).unwrap().lines().enumerate() {
            let t = line.trim_start();
            assert!(
                !t.to_lowercase().contains("house") || t.starts_with("//"),
                "{}:{} is code that mentions house: {line}",
                f.display(),
                i + 1
            );
        }
    }
}
