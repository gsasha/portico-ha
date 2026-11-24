//! Golden vectors produced by the REAL Go `internal/wsmedia` and `cmd/portico-transport/wsmedia.go`
//! (tools/gen_golden/wsmedia_gen.go, see README.md) replayed against the Rust port:
//! 24 scenarios of a `wsmedia.Leg` (every mu-law byte, every L16 sample value, the refusals, the
//! full channels, the idle watchdog, the close rules), 94 of the hub (`media_open` / `media_frame` /
//! `media_close` over a corpus of parameters, the notifications, the idle close), 61 strings through
//! `base64.StdEncoding.DecodeString` and 17 byte strings through `EncodeToString`.
//!
//! Every observation of every step must be equal to the recorded one, with these exceptions:
//! * parameters that are not valid JSON cannot be handed to the Rust hub at all (`Params` is a
//!   validated `RawValue`; the IPC layer rejects such a line before any handler runs, in Go as
//!   well), so the replay checks only that they ARE invalid JSON.
//! * an `open` whose join this file's recorder is asked to watch gets Go's `connected` event.

mod common;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::golden::{frame_of, gen_bytes, wait_until};
use serde_json::{json, Map, Value};
use tokio::sync::{watch, Notify};
use tokio::time::{sleep, timeout};
use transport_rs::call::ClientLeg;
use transport_rs::ipc::{HandlerResult, Params, Reply};
use transport_rs::util::{base64_decode, base64_encode};
use transport_rs::wsmedia::{JoinFn, Leg, MediaNotifier, SendFn, WsMediaHub};

fn load() -> Value {
    common::golden::load("wsmedia.json")
}

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len() / 2)
        .map(|i| u8::from_str_radix(&s[i * 2..i * 2 + 2], 16).unwrap())
        .collect()
}

// ---- the recorder (wmRec) ---------------------------------------------------------------------------

struct Rec {
    sent: Mutex<Vec<Vec<u8>>>,
    events: Mutex<Vec<String>>,
    gate: watch::Sender<bool>,
    in_send: Mutex<Arc<Notify>>,
}

impl Rec {
    fn new() -> Arc<Rec> {
        Arc::new(Rec {
            sent: Mutex::new(Vec::new()),
            events: Mutex::new(Vec::new()),
            gate: watch::channel(true).0,
            in_send: Mutex::new(Arc::new(Notify::new())),
        })
    }
    fn ev(&self, s: String) {
        self.events.lock().unwrap().push(s);
    }
    fn take_events(&self) -> Vec<String> {
        std::mem::take(&mut *self.events.lock().unwrap())
    }
    fn nsent(&self) -> usize {
        self.sent.lock().unwrap().len()
    }
    fn take_sent(&self) -> Vec<Value> {
        std::mem::take(&mut *self.sent.lock().unwrap())
            .iter()
            .map(|b| frame_of(b))
            .collect()
    }
    fn send_fn(self: &Arc<Self>) -> SendFn {
        let rec = self.clone();
        Arc::new(move |p: Vec<u8>| {
            let rec = rec.clone();
            Box::pin(async move {
                let mut rx = rec.gate.subscribe();
                if !*rx.borrow() {
                    rec.in_send.lock().unwrap().notify_one();
                    let _ = rx.wait_for(|open| *open).await;
                }
                rec.sent.lock().unwrap().push(p);
            })
        })
    }
}

/// `wmObs` with `omitempty`.
#[derive(Default)]
struct Obs {
    res: String,
    code: String,
    msg: String,
    result: String,
    sent: Vec<Value>,
    out: Vec<Value>,
    events: Vec<String>,
}

impl Obs {
    fn json(&self) -> Value {
        let mut m = Map::new();
        for (k, v) in [
            ("res", &self.res),
            ("code", &self.code),
            ("msg", &self.msg),
            ("result", &self.result),
        ] {
            if !v.is_empty() {
                m.insert(k.into(), json!(v));
            }
        }
        if !self.sent.is_empty() {
            m.insert("sent".into(), json!(self.sent));
        }
        if !self.out.is_empty() {
            m.insert("out".into(), json!(self.out));
        }
        if !self.events.is_empty() {
            m.insert("events".into(), json!(self.events));
        }
        Value::Object(m)
    }
}

async fn drain(leg: &Arc<Leg>) -> Vec<Value> {
    let out = ClientLeg::output(&**leg);
    let mut rx = out.lock().await;
    let mut v = Vec::new();
    while let Ok(f) = rx.try_recv() {
        v.push(frame_of(&f));
    }
    v
}

fn ms(op: &Value) -> u64 {
    op["ms"].as_u64().unwrap_or(0)
}

fn name(op: &Value) -> String {
    op["name"].as_str().unwrap_or("").to_string()
}

// ---- leg scenarios ----------------------------------------------------------------------------------

async fn run_leg(sc: &Value) {
    let rec = Rec::new();
    let mut leg: Option<Arc<Leg>> = None;
    let sname = sc["name"].as_str().unwrap();
    for (i, op) in sc["ops"].as_array().unwrap().iter().enumerate() {
        let mut o = Obs::default();
        let kind = op["op"].as_str().unwrap();
        match kind {
            "new" => {
                match Leg::new(
                    op["codec"].as_str().unwrap_or(""),
                    Duration::from_millis(ms(op)),
                    rec.send_fn(),
                ) {
                    Ok(l) => {
                        leg = Some(l);
                        o.res = "ok".into();
                    }
                    Err(e) => o.res = e,
                }
            }
            "push" => {
                o.res = match leg.as_ref().unwrap().push(&gen_bytes(&op["gen"])) {
                    Ok(()) => "ok".into(),
                    Err(e) => e.message().into(),
                }
            }
            "in" => {
                o.res = match ClientLeg::input(&**leg.as_ref().unwrap())
                    .try_send(gen_bytes(&op["gen"]))
                {
                    Ok(()) => "ok".into(),
                    Err(_) => "full".into(),
                }
            }
            "drain" => o.out = drain(leg.as_ref().unwrap()).await,
            "settle" => {
                sleep(Duration::from_millis(ms(op))).await;
                o.sent = rec.take_sent();
            }
            "wait_sent" => {
                let n = op["n"].as_u64().unwrap() as usize;
                wait_until(ms(op), || rec.nsent() >= n).await;
                o.sent = rec.take_sent();
            }
            "block_send" => {
                *rec.in_send.lock().unwrap() = Arc::new(Notify::new());
                rec.gate.send_replace(false);
            }
            "wait_in_send" => {
                let n = rec.in_send.lock().unwrap().clone();
                o.res = match timeout(Duration::from_millis(ms(op)), n.notified()).await {
                    Ok(()) => "in send".into(),
                    Err(_) => "timeout".into(),
                };
            }
            "unblock_send" => {
                rec.gate.send_replace(true);
            }
            "close" => {
                leg.as_ref()
                    .unwrap()
                    .close(op["reason"].as_str().unwrap_or(""))
                    .await
            }
            "on_closed" => {
                let (r, n) = (rec.clone(), name(op));
                leg.as_ref()
                    .unwrap()
                    .on_closed(move |reason| {
                        Box::pin(async move {
                            r.ev(format!("{n}:{reason}"));
                        })
                    })
                    .await;
            }
            "on_disc" => {
                let (r, n) = (rec.clone(), name(op));
                ClientLeg::on_disconnected(&**leg.as_ref().unwrap(), Box::new(move || r.ev(n)));
            }
            "on_connected" => {
                let (r, n) = (rec.clone(), name(op));
                ClientLeg::on_connected(&**leg.as_ref().unwrap(), Box::new(move || r.ev(n)));
            }
            "wait_events" => {
                let n = op["n"].as_u64().unwrap() as usize;
                wait_until(ms(op), || rec.events.lock().unwrap().len() >= n).await;
            }
            "sleep" => sleep(Duration::from_millis(ms(op))).await,
            "events" => {}
            k => panic!("leg op {k}"),
        }
        o.events = rec.take_events();
        assert_eq!(
            o.json(),
            sc["obs"][i],
            "{sname}: step {i} ({kind}) differs from the real Go leg"
        );
    }
    if let Some(l) = leg {
        l.close("end of scenario").await;
    }
}

// ---- hub scenarios ----------------------------------------------------------------------------------

struct Notes(Arc<Rec>);

impl MediaNotifier for Notes {
    fn notify(
        &self,
        method: &str,
        params: Value,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>> {
        // Go: marshalGo(params) of a map[string]string (keys sorted)
        let line = format!("note {method} {}", serde_json::to_string(&params).unwrap());
        self.0.ev(line);
        Box::pin(async {})
    }
}

fn raw_params(raw: &str) -> Result<Params, ()> {
    if raw.is_empty() {
        return Ok(None);
    }
    serde_json::value::RawValue::from_string(raw.to_string())
        .map(Some)
        .map_err(|_| ())
}

fn hub_obs(r: HandlerResult) -> Obs {
    let mut o = Obs::default();
    match r {
        Ok(Reply::Json(v)) => o.result = serde_json::to_string(&v).unwrap(),
        Ok(Reply::Raw(s)) => o.result = s,
        Err(e) => {
            o.code = e.code;
            o.msg = e.message;
        }
    }
    o
}

async fn run_hub(sc: &Value) {
    let rec = Rec::new();
    let mut hub: Option<Arc<WsMediaHub>> = None;
    let sname = sc["name"].as_str().unwrap();
    for (i, op) in sc["ops"].as_array().unwrap().iter().enumerate() {
        let mut o = Obs::default();
        let kind = op["op"].as_str().unwrap();
        let mut skip_compare = false;
        match kind {
            "hub_new" => {
                let watch = op["mode"] == "watch";
                let r = rec.clone();
                let join: JoinFn = Arc::new(move |call_id: &str, leg: Arc<dyn ClientLeg>| {
                    r.ev(format!("join {call_id}"));
                    if watch {
                        let (a, id) = (r.clone(), call_id.to_string());
                        leg.on_connected(Box::new(move || a.ev(format!("connected {id}"))));
                        let (a, id) = (r.clone(), call_id.to_string());
                        leg.on_disconnected(Box::new(move || a.ev(format!("disconnected {id}"))));
                    }
                });
                hub = Some(WsMediaHub::new(
                    Arc::new(Notes(rec.clone())),
                    Duration::from_millis(ms(op)),
                    join,
                ));
            }
            "open" | "frame" | "close" => {
                let raw = op["raw"].as_str().unwrap_or("");
                match raw_params(raw) {
                    Err(()) => {
                        // not valid JSON: never reaches a handler (see the file header); the Go
                        // answer is a parse error of the same kind
                        skip_compare = true;
                        assert!(
                            serde_json::from_str::<Value>(raw).is_err(),
                            "{sname}: {raw:?}"
                        );
                    }
                    Ok(p) => {
                        let h = hub.as_ref().unwrap();
                        o = hub_obs(match kind {
                            "open" => h.open(p).await,
                            "frame" => h.frame(p).await,
                            _ => h.close(p).await,
                        });
                    }
                }
            }
            "drain" | "in" => {
                let call = op["call"].as_str().unwrap();
                match hub.as_ref().unwrap().leg(call) {
                    None => o.res = "no leg".into(),
                    Some(leg) if kind == "drain" => o.out = drain(&leg).await,
                    Some(leg) => {
                        o.res = match ClientLeg::input(&*leg).try_send(gen_bytes(&op["gen"])) {
                            Ok(()) => "ok".into(),
                            Err(_) => "full".into(),
                        }
                    }
                }
            }
            "settle" | "sleep" => sleep(Duration::from_millis(ms(op))).await,
            "wait_events" => {
                let n = op["n"].as_u64().unwrap() as usize;
                wait_until(ms(op), || rec.events.lock().unwrap().len() >= n).await;
            }
            k => panic!("hub op {k}"),
        }
        o.events = rec.take_events();
        if skip_compare {
            continue;
        }
        assert_eq!(
            o.json(),
            sc["obs"][i],
            "{sname}: step {i} ({kind} {}) differs from the real Go hub",
            op["raw"].as_str().unwrap_or("")
        );
    }
}

#[tokio::test]
async fn golden_wsmedia_legs() {
    let g = load();
    let mut n = 0;
    for sc in g["scenarios"].as_array().unwrap() {
        if sc["kind"] == "leg" {
            run_leg(sc).await;
            n += 1;
        }
    }
    assert_eq!(n, 24, "the number of leg scenarios in the file changed");
}

#[tokio::test]
async fn golden_wsmedia_hub() {
    let g = load();
    let mut n = 0;
    for sc in g["scenarios"].as_array().unwrap() {
        if sc["kind"] == "hub" {
            run_hub(sc).await;
            n += 1;
        }
    }
    assert_eq!(n, 94, "the number of hub scenarios in the file changed");
}

#[test]
fn golden_wsmedia_base64() {
    let g = load();
    let dec = g["base64"].as_array().unwrap();
    assert_eq!(dec.len(), 61);
    let mut bad = Vec::new();
    for v in dec {
        let input = v["in"].as_str().unwrap();
        let got = base64_decode(input);
        let want = v["ok"]
            .as_bool()
            .unwrap()
            .then(|| unhex(v["hex"].as_str().unwrap_or("")));
        if got != want {
            bad.push(format!("{input:?}: got {got:?}, Go {want:?}"));
        }
    }
    assert!(bad.is_empty(), "{}", bad.join("\n"));
    let enc = g["encode"].as_array().unwrap();
    assert_eq!(enc.len(), 17);
    for v in enc {
        assert_eq!(
            base64_encode(&unhex(v["hex"].as_str().unwrap())),
            v["out"].as_str().unwrap()
        );
    }
}
