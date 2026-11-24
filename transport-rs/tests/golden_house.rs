//! Golden vectors produced by the REAL Go `internal/house` and `cmd/portico-transport/house.go`
//! (tools/gen_golden/house_gen.go, see README.md) replayed against the Rust port: the scenarios
//! of the Manager with scripted legs (validation, caps, owners, ids, holds, offers, the watchdog,
//! shutdown, the counters line with its duration and gap text) and the scenarios of the verbs
//! (`house_open` over a corpus of parameters, `house_close`, the `media_frame` / `media_close`
//! router, the real frame-channel legs and the real WebRTC session).
//!
//! Every observation of every step must be equal to the recorded one. The events and counters
//! lines of a step are reported only at a `wait_events` step and at the end of the scenario
//! (sorted: the order between notifications written by different tasks is not defined in Go either).
//! Real time, as in `golden_wsmedia.rs`: the waits are generous.

mod common;

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::golden::{frame_of, gen_bytes, wait_until};
use common::house::*;
use serde_json::{json, Map, Value};
use tokio::time::{sleep, timeout, Instant};
use transport_rs::call::ClientLeg;
use transport_rs::house::{
    BoxFut, Config, EmitFn, Event, FrameError, HouseError, HouseHub, HouseLeg, LegSpec, Manager,
};
use transport_rs::ipc::{HandlerResult, Params, Reply};
use transport_rs::sfu::session::SessionConfig;
use transport_rs::wsmedia::{JoinFn, MediaNotifier, PushError, WsMediaHub};

/// `hsSettle` / `hsEnd` in house_gen.go.
const SETTLE: Duration = Duration::from_millis(15);
const END: Duration = Duration::from_millis(150);

fn load() -> Value {
    common::golden::load("house.json")
}

fn ms_of(op: &Value) -> u64 {
    op["ms"].as_u64().unwrap_or(0)
}

/// `hsRec`
#[derive(Default)]
struct Rec {
    events: Mutex<Vec<String>>,
    logs: Mutex<Vec<String>>,
}

impl Rec {
    fn ev(&self, s: String) {
        self.events.lock().unwrap().push(s);
    }
    fn nev(&self) -> usize {
        self.events.lock().unwrap().len()
    }
    fn take(&self) -> (Vec<String>, Vec<String>) {
        let mut e = std::mem::take(&mut *self.events.lock().unwrap());
        let mut l = std::mem::take(&mut *self.logs.lock().unwrap());
        e.sort();
        l.sort();
        (e, l)
    }
}

/// `hsObs` with `omitempty` (and `count`, which is always there).
#[derive(Default)]
struct Obs {
    res: String,
    code: String,
    msg: String,
    result: String,
    count: usize,
    out: Vec<Value>,
    events: Vec<String>,
    logs: Vec<String>,
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
        m.insert("count".into(), json!(self.count));
        if !self.out.is_empty() {
            m.insert("out".into(), json!(self.out));
        }
        if !self.events.is_empty() {
            m.insert("events".into(), json!(self.events));
        }
        if !self.logs.is_empty() {
            m.insert("logs".into(), json!(self.logs));
        }
        Value::Object(m)
    }
}

// ---- core scenarios ------------------------------------------------------------------------------------

fn specs_of(v: &Value) -> Vec<LegSpec> {
    v.as_array()
        .map(|a| {
            a.iter()
                .map(|s| LegSpec {
                    side: s["side"].as_str().unwrap_or("").into(),
                    kind: s["kind"].as_str().unwrap_or("").into(),
                    codec: s["codec"].as_str().unwrap_or("").into(),
                    sdp: s["sdp"].as_str().unwrap_or("").into(),
                    owner: s["owner"].as_str().unwrap_or("").into(),
                })
                .collect()
        })
        .unwrap_or_default()
}

fn err_kind(e: &HouseError) -> &'static str {
    match e {
        HouseError::BadParams(_) => "bad_params",
        HouseError::TooMany(_) => "too_many",
        HouseError::AlreadyOpen => "already_open",
        HouseError::Shutdown => "shutdown",
        HouseError::Failed(_) => "failed",
    }
}

async fn run_core(sc: &Value) {
    let sname = sc["name"].as_str().unwrap();
    let rec = Arc::new(Rec::default());
    let clock = Arc::new(AtomicU64::new(1_792_670_400_000_000_000));
    let mut mgr: Option<Arc<Manager>> = None;
    let mut fac = Arc::new(FakeFactory::default());
    let mut sessions: HashMap<String, String> = HashMap::new();
    let ops = sc["ops"].as_array().unwrap();
    let obs = sc["obs"].as_array().unwrap();
    assert_eq!(obs.len(), ops.len() + 1, "{sname}: ops and observations");
    for (i, op) in ops.iter().enumerate() {
        let kind = op["op"].as_str().unwrap();
        let mut o = Obs::default();
        let s = |k: &str| op[k].as_str().unwrap_or("").to_string();
        let leg_of =
            |call: &str, side: &str, sessions: &HashMap<String, String>, fac: &FakeFactory| {
                fac.leg(&format!("{}-{side}", sessions[call]))
            };
        match kind {
            "mgr_new" => {
                fac = Arc::new(FakeFactory::default());
                {
                    let mut g = fac.st.lock().unwrap();
                    g.fail_on = s("fail_on");
                    if !s("offer_err").is_empty() {
                        g.offer_err = Some(s("offer_err"));
                    }
                    g.born_closed = op["born_closed"].as_bool().unwrap_or(false);
                    if !s("gate_leg").is_empty() {
                        g.gate.insert(s("gate_leg"), Gate::new());
                    }
                }
                let ids: Vec<String> = op["ids"]
                    .as_array()
                    .map(|a| a.iter().map(|x| x.as_str().unwrap().to_string()).collect())
                    .unwrap_or_default();
                let n = Arc::new(AtomicUsize::new(0));
                let mut cfg = Config {
                    max_bridges: op["max_bridges"].as_u64().unwrap_or(0) as usize,
                    max_per_owner: op["max_owner"].as_u64().unwrap_or(0) as usize,
                    silence: Duration::from_millis(ms_of(op)),
                    new_id: Some(Arc::new(move || {
                        let k = n.fetch_add(1, Ordering::SeqCst) + 1;
                        if k <= ids.len() {
                            ids[k - 1].clone()
                        } else {
                            format!("{k:016x}")
                        }
                    })),
                    ..Default::default()
                };
                if op["clock"].as_bool().unwrap_or(false) {
                    let c = clock.clone();
                    cfg.now = Some(Arc::new(move || c.load(Ordering::SeqCst)));
                }
                if op["log"].as_bool().unwrap_or(false) {
                    let r = rec.clone();
                    cfg.logf = Some(Arc::new(move |l| r.logs.lock().unwrap().push(l)));
                }
                let r = rec.clone();
                let emit: EmitFn = Arc::new(move |e: Event| -> BoxFut {
                    r.ev(format!(
                        "{} {} {} reason={} sdp={}",
                        e.call_id, e.state, e.side, e.reason, e.sdp
                    ));
                    Box::pin(async {})
                });
                mgr = Some(Manager::new(cfg, Arc::new(Ff(fac.clone())), emit));
            }
            "set_fail" => fac.st.lock().unwrap().fail_on = s("fail_on"),
            "open" => {
                let call = s("call");
                match mgr
                    .as_ref()
                    .unwrap()
                    .open(&call, specs_of(&op["specs"]))
                    .await
                {
                    Err(e) => o.res = format!("{}: {}", err_kind(&e), e.message()),
                    Ok(r) => {
                        sessions.insert(call, r.session.clone());
                        let legs: Vec<Value> = r
                            .legs
                            .iter()
                            .map(|l| {
                                json!({"side": l.side, "kind": l.kind, "leg_id": l.leg_id, "codec": l.codec})
                            })
                            .collect();
                        o.result =
                            serde_json::to_string(&json!({"session": r.session, "legs": legs}))
                                .unwrap();
                    }
                }
            }
            "up" => {
                let leg = leg_of(&s("call"), &s("side"), &sessions, &fac);
                o.res = match timeout(
                    Duration::from_secs(1),
                    leg.out_tx.send(gen_bytes(&op["gen"])),
                )
                .await
                {
                    Ok(Ok(())) => "ok".into(),
                    _ => "timeout".into(),
                };
            }
            "down" => {
                let leg = leg_of(&s("call"), &s("side"), &sessions, &fac);
                let want = op["n"].as_u64().unwrap_or(0) as usize;
                let mut rx = leg.in_rx.lock().await;
                wait_until(500, || rx.len() >= want).await;
                while let Ok(f) = rx.try_recv() {
                    o.out.push(frame_of(&f));
                }
            }
            "push_frame" => {
                o.res = match mgr
                    .as_ref()
                    .unwrap()
                    .frame(&s("leg"), &gen_bytes(&op["gen"]))
                {
                    Ok(()) => "ok".into(),
                    Err(FrameError::UnknownLeg) => "unknown_leg".into(),
                    Err(FrameError::Leg(PushError::BadFrame)) => "leg: bad frame".into(),
                    Err(FrameError::Leg(PushError::Closed)) => "leg: closed".into(),
                }
            }
            "close" => o.res = mgr.as_ref().unwrap().close(&s("call")).await.to_string(),
            "close_leg" => {
                o.res = mgr
                    .as_ref()
                    .unwrap()
                    .close_leg(&s("leg"), &s("reason"))
                    .await
                    .to_string()
            }
            "hold" => {
                o.res = mgr
                    .as_ref()
                    .unwrap()
                    .hold(&s("call"), &s("side"), op["on"].as_bool().unwrap_or(false))
                    .to_string()
            }
            "close_all" => mgr.as_ref().unwrap().close_all().await,
            "leg_close" => {
                let leg = leg_of(&s("call"), &s("side"), &sessions, &fac);
                HouseLeg::close(&*leg, &s("reason")).await;
            }
            "leg_connect" => leg_of(&s("call"), &s("side"), &sessions, &fac).connect(),
            "release_gate" => {
                let g = fac.st.lock().unwrap().gate.get(&s("leg")).cloned().unwrap();
                g.open();
            }
            "advance" => {
                let d = ms_of(op) * 1_000_000 + op["us"].as_u64().unwrap_or(0) * 1_000;
                clock.fetch_add(d, Ordering::SeqCst);
            }
            "sleep" => sleep(Duration::from_millis(ms_of(op))).await,
            "wait_events" => {
                let n = op["n"].as_u64().unwrap() as usize;
                wait_until(ms_of(op), || rec.nev() >= n).await;
            }
            k => panic!("core op {k}"),
        }
        if kind == "wait_events" {
            sleep(SETTLE).await;
            (o.events, o.logs) = rec.take();
        } else if kind != "sleep" {
            sleep(SETTLE).await;
        }
        let m = mgr.clone().unwrap();
        check_step(sname, i, kind, &mut o, move || m.count(), &obs[i]).await;
    }
    sleep(END).await;
    let m = mgr.as_ref().unwrap();
    let mut o = Obs::default();
    (o.events, o.logs) = rec.take();
    o.count = m.count();
    assert_eq!(
        o.json(),
        obs[ops.len()],
        "{sname}: the end of the scenario differs from the real Go manager"
    );
    m.close_all().await;
}

/// Compares one step. `count` is read again for up to a second while it differs from the
/// recorded one (an end that is still on its way); everything else must be equal at once.
async fn check_step(
    sname: &str,
    i: usize,
    kind: &str,
    o: &mut Obs,
    count: impl Fn() -> usize,
    want: &Value,
) {
    o.count = count();
    let end = Instant::now() + Duration::from_secs(1);
    while want["count"].as_u64() != Some(o.count as u64) && Instant::now() < end {
        sleep(Duration::from_millis(5)).await;
        o.count = count();
    }
    assert_eq!(
        &o.json(),
        want,
        "{sname}: step {i} ({kind}) differs from the real Go code"
    );
}

// ---- hub scenarios -------------------------------------------------------------------------------------

struct Notes {
    rec: Arc<Rec>,
    norm: Arc<Norm>,
}

impl MediaNotifier for Notes {
    fn notify(&self, method: &str, params: Value) -> BoxFut {
        let line = format!(
            "note {method} {}",
            self.norm.apply(&serde_json::to_string(&params).unwrap())
        );
        self.rec.ev(line);
        Box::pin(async {})
    }
}

/// `hsHubRun.norm` / `subst`: the random session ids become house-S1, house-S2, ... in order of
/// first appearance, and an answer SDP becomes `<answer>`.
#[derive(Default)]
struct Norm {
    sids: Mutex<Vec<String>>,
}

impl Norm {
    fn apply(&self, s: &str) -> String {
        let s = replace_sdp(s);
        let b = s.as_bytes();
        let mut out = String::new();
        let mut i = 0;
        while i < b.len() {
            if s[i..].starts_with("house-")
                && i + 22 <= b.len()
                && b[i + 6..i + 22]
                    .iter()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(c))
            {
                let id = s[i..i + 22].to_string();
                let mut g = self.sids.lock().unwrap();
                let n = match g.iter().position(|x| *x == id) {
                    Some(p) => p + 1,
                    None => {
                        g.push(id);
                        g.len()
                    }
                };
                out.push_str(&format!("house-S{n}"));
                i += 22;
            } else {
                let ch = s[i..].chars().next().unwrap();
                out.push(ch);
                i += ch.len_utf8();
            }
        }
        out
    }

    fn subst(&self, raw: &str) -> String {
        let mut raw = raw.to_string();
        for (k, id) in self.sids.lock().unwrap().iter().enumerate() {
            raw = raw.replace(&format!("{{S{}}}", k + 1), id);
        }
        raw
    }
}

/// `"sdp":"[^"]*"` -> `"sdp":"<answer>"`
fn replace_sdp(s: &str) -> String {
    let key = "\"sdp\":\"";
    let mut out = String::new();
    let mut rest = s;
    while let Some(p) = rest.find(key) {
        out.push_str(&rest[..p + key.len()]);
        let tail = &rest[p + key.len()..];
        match tail.find('"') {
            Some(q) => {
                out.push_str("<answer>\"");
                rest = &tail[q + 1..];
            }
            None => {
                rest = tail;
                break;
            }
        }
    }
    out.push_str(rest);
    out
}

fn raw_params(raw: &str) -> Params {
    if raw.is_empty() {
        return None;
    }
    Some(
        serde_json::value::RawValue::from_string(raw.to_string())
            .expect("the corpus is valid JSON"),
    )
}

fn hub_obs(norm: &Norm, r: HandlerResult) -> Obs {
    let mut o = Obs::default();
    match r {
        Ok(Reply::Json(v)) => o.result = norm.apply(&serde_json::to_string(&v).unwrap()),
        Ok(Reply::Raw(s)) => o.result = norm.apply(&s),
        Err(e) => {
            o.code = e.code;
            o.msg = norm.apply(&e.message);
        }
    }
    o
}

async fn run_hub(sc: &Value) {
    let sname = sc["name"].as_str().unwrap();
    let rec = Arc::new(Rec::default());
    let norm = Arc::new(Norm::default());
    let mut hub: Option<Arc<HouseHub>> = None;
    let mut door: Option<Arc<WsMediaHub>> = None;
    let ops = sc["ops"].as_array().unwrap();
    let obs = sc["obs"].as_array().unwrap();
    assert_eq!(obs.len(), ops.len() + 1, "{sname}: ops and observations");
    for (i, op) in ops.iter().enumerate() {
        let kind = op["op"].as_str().unwrap();
        let mut o = Obs::default();
        let params = || raw_params(&norm.subst(op["raw"].as_str().unwrap_or("")));
        match kind {
            "hub_new" => {
                let notes = Arc::new(Notes {
                    rec: rec.clone(),
                    norm: norm.clone(),
                });
                let r = rec.clone();
                let join: JoinFn = Arc::new(move |call_id: &str, _leg: Arc<dyn ClientLeg>| {
                    r.ev(format!("join {call_id}"));
                });
                let d = WsMediaHub::new(notes.clone(), Duration::from_secs(60), join);
                let idle = match ms_of(op) {
                    0 => Duration::from_secs(60),
                    n => Duration::from_millis(n),
                };
                hub = Some(HouseHub::new(
                    notes,
                    d.clone(),
                    idle,
                    SessionConfig::loopback(),
                ));
                door = Some(d);
            }
            "open" => o = hub_obs(&norm, hub.as_ref().unwrap().open(params()).await),
            "close" => o = hub_obs(&norm, hub.as_ref().unwrap().close(params()).await),
            "frame" => o = hub_obs(&norm, hub.as_ref().unwrap().route_frame(params()).await),
            "mclose" => o = hub_obs(&norm, hub.as_ref().unwrap().route_close(params()).await),
            "dopen" => o = hub_obs(&norm, door.as_ref().unwrap().open(params()).await),
            "stop" => hub.as_ref().unwrap().stop().await,
            "sleep" => sleep(Duration::from_millis(ms_of(op))).await,
            "wait_events" => {
                let n = op["n"].as_u64().unwrap() as usize;
                wait_until(ms_of(op), || rec.nev() >= n).await;
            }
            k => panic!("hub op {k}"),
        }
        if kind == "wait_events" {
            sleep(SETTLE).await;
            o.events = rec.take().0;
        } else if kind != "sleep" {
            sleep(SETTLE).await;
        }
        let h = hub.clone();
        check_step(
            sname,
            i,
            kind,
            &mut o,
            move || h.as_ref().map(|h| h.manager().count()).unwrap_or(0),
            &obs[i],
        )
        .await;
    }
    sleep(END).await;
    let h = hub.as_ref().unwrap();
    let o = Obs {
        events: rec.take().0,
        count: h.manager().count(),
        ..Default::default()
    };
    assert_eq!(
        o.json(),
        obs[ops.len()],
        "{sname}: the end of the scenario differs from the real Go hub"
    );
    h.stop().await;
}

#[tokio::test]
async fn golden_house_manager() {
    let g = load();
    let mut n = 0;
    for sc in g["scenarios"].as_array().unwrap() {
        if sc["kind"] == "core" {
            run_core(sc).await;
            n += 1;
        }
    }
    assert_eq!(n, 31, "the number of manager scenarios in the file changed");
}

#[tokio::test]
async fn golden_house_hub() {
    let g = load();
    let mut n = 0;
    for sc in g["scenarios"].as_array().unwrap() {
        if sc["kind"] == "hub" {
            run_hub(sc).await;
            n += 1;
        }
    }
    assert_eq!(n, 13, "the number of hub scenarios in the file changed");
}

/// The corpus really is a corpus: a count of the parameters the replay pushed through `house_open`.
#[test]
fn golden_house_corpus_size() {
    let g = load();
    let sc = g["scenarios"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| {
            s["name"]
                .as_str()
                .unwrap()
                .starts_with("house_open: a corpus")
        })
        .unwrap();
    let n = sc["ops"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|o| o["op"] == "open")
        .count();
    assert_eq!(n, 58, "the corpus of house_open parameters changed size");
}
