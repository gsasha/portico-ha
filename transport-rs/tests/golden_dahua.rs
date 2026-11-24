//! Golden vectors produced by the REAL Go `pkg/driver/dahua` and `internal/call/relay.go`
//! (tools/gen_golden/dahua_gen.go, see README.md) replayed against the Rust port: `mediaHost`
//! over a corpus, `Configure` over a corpus, the pre-Start behaviour, and 29 scenarios in which
//! the driver and the relay talk to a scripted fake station over UDP loopback (every datagram the
//! code sent, every event it reported, every return value and error text). The Rust station
//! below does what the Go one does, line for line; the replay requires every recorded result to
//! be equal, bar the one listed in `KNOWN_DIFFERENCES`.

mod common;

use std::collections::{HashMap, HashSet};
use std::net::{Ipv6Addr, SocketAddr};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::dahua::{echo, header};
use md5::{Digest, Md5};
use serde_json::{json, Map, Value};
use tokio::net::UdpSocket;
use tokio::time::{sleep, timeout, Instant};
use transport_rs::call::{media_host, Relay};
use transport_rs::cancel::CancelToken;
use transport_rs::driver::dahua::Dahua;
use transport_rs::driver::{Driver, ErrorReporter, EventSink, MediaTarget, PendingRinger};
use transport_rs::sip::{build_sdp, parse_rtp_packet, Endpoint, IncomingRequest, RtpPacket};

fn load(name: &str) -> Value {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("tests/golden");
    p.push(name);
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    serde_json::from_str(&text).unwrap()
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

// ---- mediaHost ------------------------------------------------------------------------------

/// Go's `ParseIP` accepts every IPv4-mapped IPv6 literal and writes it into `c=IN IP4 ...`; this
/// port refuses them (PARITY.md difference 99). Those vectors are checked for exactly that.
fn is_ipv4_mapped_literal(h: &str) -> bool {
    h.parse::<Ipv6Addr>()
        .map(|a| a.to_ipv4_mapped().is_some())
        .unwrap_or(false)
}

#[test]
fn golden_media_host() {
    let g = load("dahua_driver.json");
    let (mut n, mut mapped) = (0, 0);
    for v in g["media_host"].as_array().unwrap() {
        let (configured, routed) = (s(v, "configured"), s(v, "routed"));
        let got = media_host(configured, routed);
        let chosen = if configured.is_empty() {
            routed
        } else {
            configured
        };
        n += 1;
        if s(v, "err").is_empty() && is_ipv4_mapped_literal(chosen) {
            // Go: accepted. Rust: refused with the standard text.
            mapped += 1;
            let want = format!(
                "call: {} is not a usable IPv4 media host for the device leg's SDP",
                transport_rs::gostd::strconv::go_quote(chosen)
            );
            assert_eq!(got, Err(want), "{configured:?} {routed:?}");
            continue;
        }
        match got {
            Ok(h) => {
                assert_eq!(
                    s(v, "err"),
                    "",
                    "{configured:?} {routed:?} accepted by Rust"
                );
                assert_eq!(h, s(v, "out"), "{configured:?} {routed:?}");
            }
            Err(e) => assert_eq!(e, s(v, "err"), "{configured:?} {routed:?}"),
        }
    }
    assert_eq!(n, 36, "only {n} vectors"); // exact (R2-107): a truncated golden file must fail
    assert!(
        mapped >= 3,
        "the IPv4-mapped vectors are missing ({mapped})"
    );
}

// ---- Configure, Capabilities, the pre-Start behaviour ----------------------------------------

#[tokio::test]
async fn golden_configure_and_capabilities() {
    let g = load("dahua_driver.json");
    for v in g["configure"].as_array().unwrap() {
        let name = s(v, "name");
        let cfg: Map<String, Value> = serde_json::from_str(v["cfg"].to_string().as_str()).unwrap();
        let mut d = Dahua::default();
        let r = d.configure(&cfg);
        match r {
            Ok(()) => assert_eq!(s(v, "err"), "", "{name}"),
            Err(e) => assert_eq!(e.message(), s(v, "err"), "{name}"),
        }
        assert_eq!(d.capabilities().to_wire(), s(v, "caps"), "{name}");
    }
    let pre = &g["pre_start"];
    let d = Dahua::default();
    let ctx = CancelToken::new();
    let e = d.answer(&ctx, MediaTarget::default()).await.unwrap_err();
    assert_eq!(e.message(), s(pre, "answer_err"));
    assert_eq!(
        d.pending_ring().is_some(),
        pre["pending_ring"].as_bool().unwrap()
    );
    assert_eq!(
        d.pending_ring_for("x").is_some(),
        pre["pending_ring_for"].as_bool().unwrap()
    );
    let e = d.open_relay(&ctx).await.unwrap_err();
    assert_eq!(e.message(), s(pre, "open_relay_err"));
    assert!(d.stop().await.is_ok());
    assert!(d.stop().await.is_ok());
}

// ---- the masker (the same scan as dahua_gen.go) ----------------------------------------------

struct Masker {
    known: HashSet<String>,
    b: HashMap<String, String>,
    t: HashMap<String, String>,
    c: HashMap<String, String>,
    mask_media: bool,
}

fn alnum(c: u8) -> bool {
    c.is_ascii_alphanumeric()
}

fn tag_char(c: u8) -> bool {
    !b";>\r\n ,".contains(&c)
}

/// Replaces each run that follows `key` (a run of bytes accepted by `ok`) by `f(run)`.
fn scan(s: &str, key: &str, ok: fn(u8) -> bool, f: &mut dyn FnMut(&str) -> String) -> String {
    let mut out = String::new();
    let mut rest = s;
    while let Some(i) = rest.find(key) {
        out.push_str(&rest[..i + key.len()]);
        rest = &rest[i + key.len()..];
        let n = rest.bytes().take_while(|&c| ok(c)).count();
        out.push_str(&f(&rest[..n]));
        rest = &rest[n..];
    }
    out.push_str(rest);
    out
}

fn ordinal(tbl: &mut HashMap<String, String>, tok: &str, prefix: &str) -> String {
    if let Some(v) = tbl.get(tok) {
        return v.clone();
    }
    let v = format!("<{prefix}{}>", tbl.len() + 1);
    tbl.insert(tok.to_string(), v.clone());
    v
}

impl Masker {
    fn new(mask_media: bool) -> Masker {
        Masker {
            known: HashSet::new(),
            b: HashMap::new(),
            t: HashMap::new(),
            c: HashMap::new(),
            mask_media,
        }
    }

    fn learn(&mut self, s: &str) {
        let mut known = std::mem::take(&mut self.known);
        scan(s, "branch=z9hG4bK", alnum, &mut |t| {
            known.insert(format!("z9hG4bK{t}"));
            t.to_string()
        });
        scan(s, "tag=", tag_char, &mut |t| {
            known.insert(t.to_string());
            t.to_string()
        });
        for l in s.split("\r\n") {
            if let Some(v) = l.strip_prefix("Call-ID:") {
                known.insert(v.trim().to_string());
            }
        }
        self.known = known;
    }

    fn call(&mut self, id: &str) -> String {
        if self.known.contains(id) {
            id.to_string()
        } else {
            ordinal(&mut self.c, id, "C")
        }
    }

    fn mask(&mut self, s: &str) -> String {
        let known = self.known.clone();
        let b = &mut self.b;
        let s = scan(s, "branch=z9hG4bK", alnum, &mut |t| {
            if known.contains(&format!("z9hG4bK{t}")) {
                t.to_string()
            } else {
                ordinal(b, t, "B")
            }
        });
        let t = &mut self.t;
        let s = scan(&s, "tag=", tag_char, &mut |tok| {
            if known.contains(tok) {
                tok.to_string()
            } else {
                ordinal(t, tok, "T")
            }
        });
        let lines: Vec<String> = s
            .split("\r\n")
            .map(|l| match l.strip_prefix("Call-ID:") {
                Some(v) => format!("Call-ID: {}", self.call(v.trim())),
                None => l.to_string(),
            })
            .collect();
        let s = lines.join("\r\n");
        let s = scan(&s, "127.0.0.1:", |c| c.is_ascii_digit(), &mut |_| {
            "<P>".to_string()
        });
        if self.mask_media {
            scan(&s, "m=audio ", |c| c.is_ascii_digit(), &mut |_| {
                "<MP>".to_string()
            })
        } else {
            s
        }
    }
}

// ---- the station -----------------------------------------------------------------------------

#[derive(Default)]
struct StState {
    inbox: Vec<String>,
    events: Vec<String>,
    reg_mode: String,
    inv_mode: String,
    drv: Option<SocketAddr>,
    regs: u32,
    deregs: u32,
    last_inv: String,
    relay_port: u16,
    masker: Option<Masker>,
}

struct Station {
    sip: Arc<UdpSocket>,
    rtp: Arc<UdpSocket>,
    st: Arc<Mutex<StState>>,
    task: tokio::task::JoinHandle<()>,
}

impl Station {
    async fn new(mask_media: bool) -> Station {
        let sip = Arc::new(UdpSocket::bind("127.0.0.1:0").await.unwrap());
        let rtp = Arc::new(UdpSocket::bind("127.0.0.1:0").await.unwrap());
        let st = Arc::new(Mutex::new(StState {
            reg_mode: "ok".into(),
            inv_mode: "none".into(),
            masker: Some(Masker::new(mask_media)),
            ..Default::default()
        }));
        let (s2, r2, st2) = (sip.clone(), rtp.clone(), st.clone());
        let task = tokio::spawn(async move {
            let mut buf = vec![0u8; 8192];
            loop {
                let Ok((n, from)) = s2.recv_from(&mut buf).await else {
                    return;
                };
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                let (reg_mode, inv_mode) = {
                    let mut g = st2.lock().unwrap();
                    if g.drv.is_none() {
                        g.drv = Some(from);
                    }
                    (g.reg_mode.clone(), g.inv_mode.clone())
                };
                let send = |x: String| {
                    let s3 = s2.clone();
                    async move {
                        let _ = s3.send_to(x.as_bytes(), from).await;
                    }
                };
                if req.starts_with("REGISTER ") {
                    match reg_mode.as_str() {
                        "403" => send(echo(&req, 403, "Forbidden", "", "", &[])).await,
                        "486" => send(echo(&req, 486, "Busy Here", "", "", &[])).await,
                        "silent" => {}
                        _ => {
                            if !req.contains("Authorization:") {
                                send(echo(&req, 401, "Unauthorized", "", "",
                                    &[r#"WWW-Authenticate: Digest realm="r", nonce="n1", algorithm=MD5"#])).await;
                            } else if req.contains("Expires: 0") {
                                st2.lock().unwrap().deregs += 1;
                                send(echo(&req, 200, "OK", "", "", &["Expires: 0"])).await;
                            } else {
                                st2.lock().unwrap().regs += 1;
                                send(echo(&req, 200, "OK", "", "", &["Expires: 3600"])).await;
                            }
                        }
                    }
                    continue;
                } else if req.starts_with("INVITE ") {
                    st2.lock().unwrap().last_inv = header(&req, "Call-ID");
                    match inv_mode.as_str() {
                        "200" => {
                            let ra = r2.local_addr().unwrap();
                            let sdp = build_sdp(&ra.ip().to_string(), ra.port() as i64);
                            send(echo(&req, 200, "OK", "devicetag", &sdp, &[])).await;
                        }
                        "486" => send(echo(&req, 486, "Busy Here", "devicetag", "", &[])).await,
                        _ => {}
                    }
                } else if req.starts_with("BYE ") {
                    send(echo(&req, 200, "OK", "", "", &[])).await;
                }
                let mut g = st2.lock().unwrap();
                if req.starts_with("INVITE ") || req.starts_with("SIP/2.0 200") {
                    if let Some(i) = req.find("m=audio ") {
                        let rest = &req[i + "m=audio ".len()..];
                        if let Some(j) = rest.find(' ') {
                            if let Ok(p) = rest[..j].parse::<u16>() {
                                g.relay_port = p;
                            }
                        }
                    }
                }
                g.inbox.push(req);
            }
        });
        Station { sip, rtp, st, task }
    }

    fn port(&self) -> u16 {
        self.sip.local_addr().unwrap().port()
    }

    fn mask(&self, x: &str) -> String {
        let mut g = self.st.lock().unwrap();
        g.masker.as_mut().unwrap().mask(x)
    }

    fn event(&self, e: String) {
        self.st.lock().unwrap().events.push(e);
    }

    fn call_mask(&self, id: &str) -> String {
        let mut g = self.st.lock().unwrap();
        g.masker.as_mut().unwrap().call(id)
    }
}

impl Drop for Station {
    fn drop(&mut self) {
        self.task.abort();
    }
}

struct Sink(Arc<Station>);

impl EventSink for Sink {
    fn ringing(&self, call_id: &str, device_id: &str) {
        self.0
            .event(format!("ringing|{}|{device_id}", self.0.call_mask(call_id)));
    }
    fn state_changed(&self, call_id: &str, state: &str) {
        self.0
            .event(format!("state|{}|{state}", self.0.call_mask(call_id)));
    }
    fn availability_changed(&self, id: &str, ok: bool, reason: &str) {
        self.0.event(format!("avail|{id}|{ok}|{reason}"));
    }
    fn error_reporter(&self) -> Option<&dyn ErrorReporter> {
        Some(self)
    }
}

impl ErrorReporter for Sink {
    fn driver_error(&self, id: &str, code: &str, msg: &str) {
        self.0.event(format!("error|{id}|{code}|{msg}"));
    }
}

// ---- the replay ------------------------------------------------------------------------------

fn md5hex(b: &[u8]) -> String {
    Md5::digest(b).iter().map(|b| format!("{b:02x}")).collect()
}

fn frame(seed: u8) -> Vec<u8> {
    (0..1280usize)
        .map(|i| seed.wrapping_add((i % 7) as u8))
        .collect()
}

/// Known differences from Go, by (scenario, op index): the recorded Go value contains `go`, and
/// Rust's equals `rust`. Every one is a numbered difference in PARITY.md.
const KNOWN_DIFFERENCES: &[(&str, usize, &str, &str)] = &[
    // difference 98: a second Stop is a no-op (Go returns the socket's "use of closed" error)
    (
        "stop twice is fine",
        4,
        "use of closed network connection",
        "",
    ),
];

async fn run_scenario(sc: &Value) -> Vec<Value> {
    let kind = s(sc, "kind");
    let mask_media = sc["mask_media"].as_bool().unwrap_or(false);
    let st = Arc::new(Station::new(mask_media).await);
    let ops = sc["ops"].as_array().unwrap();
    for o in ops {
        if s(o, "op") == "send" {
            st.st
                .lock()
                .unwrap()
                .masker
                .as_mut()
                .unwrap()
                .learn(s(o, "raw"));
        }
    }
    let port = st.port();
    let e = |st: &Station, err: String| st.mask(&err);
    let mut drv: Option<Arc<Dahua>> = None;
    let mut ep: Option<Endpoint> = None;
    let mut relay: Option<Relay> = None;
    let mut caught: Option<tokio::sync::mpsc::Receiver<IncomingRequest>> = None;
    let mut dials: HashMap<String, tokio::task::JoinHandle<Result<(), String>>> = HashMap::new();
    let mut dial_res: HashMap<String, Value> = HashMap::new();
    let ctx = CancelToken::new();
    let mut results = Vec::new();
    assert!(kind == "driver" || kind == "relay");

    for o in ops {
        let mut res = Value::Null;
        match s(o, "op") {
            "set" => {
                let mut g = st.st.lock().unwrap();
                if s(o, "call") == "reg" {
                    g.reg_mode = s(o, "a").to_string();
                } else {
                    g.inv_mode = s(o, "a").to_string();
                }
            }
            "start" => {
                let mut d = Dahua::default();
                d.set_sip_port(port as i64);
                let cfg = sc["cfg"].as_object().cloned().unwrap_or_default();
                match d.configure(&cfg) {
                    Err(er) => res = json!({"err": er.message()}),
                    Ok(()) => {
                        let d = Arc::new(d);
                        let r = d.start(&ctx, Arc::new(Sink(st.clone()))).await;
                        res = json!({"err": r.err().map(|x| x.message().to_string()).unwrap_or_default()});
                        drv = Some(d);
                    }
                }
            }
            "wait_registered" => {
                let end = Instant::now() + Duration::from_millis(o["ms"].as_u64().unwrap());
                loop {
                    let n = st.st.lock().unwrap().regs;
                    if n > 0 || Instant::now() > end {
                        res = json!({"registered": n > 0});
                        break;
                    }
                    sleep(Duration::from_millis(10)).await;
                }
            }
            "send" => {
                let (mut to, inv) = {
                    let g = st.st.lock().unwrap();
                    (g.drv, g.last_inv.clone())
                };
                let mut raw = s(o, "raw").replace("{{CALLID}}", &inv);
                if raw.contains("{{SDP}}") {
                    let ra = st.rtp.local_addr().unwrap();
                    let body = build_sdp("127.0.0.1", ra.port() as i64);
                    raw = raw
                        .replace("{{CL}}", &body.len().to_string())
                        .replace("{{SDP}}", &body);
                }
                if let Some(ep) = &ep {
                    raw = raw.replace("{{EP}}", ep.local_addr());
                    to = Some(ep.local_addr().parse().unwrap());
                }
                let to = to.expect("send before the code spoke");
                st.sip.send_to(raw.as_bytes(), to).await.unwrap();
            }
            "step" => {
                sleep(Duration::from_millis(o["ms"].as_u64().unwrap())).await;
                let (inbox, events) = {
                    let mut g = st.st.lock().unwrap();
                    (std::mem::take(&mut g.inbox), std::mem::take(&mut g.events))
                };
                let msgs: Vec<String> = inbox.iter().map(|m| st.mask(m)).collect();
                res = json!({"msgs": msgs, "events": events});
            }
            "answer" => {
                let r = drv
                    .as_ref()
                    .unwrap()
                    .answer(
                        &ctx,
                        MediaTarget {
                            host: s(o, "host").to_string(),
                            port: o["port"].as_i64().unwrap_or(0),
                        },
                    )
                    .await;
                res = json!({"err": e(&st, r.err().map(|x| x.message().to_string()).unwrap_or_default())});
            }
            "pending_ring" | "pending_ring_for" => {
                let d = drv.as_ref().unwrap();
                let req = if s(o, "op") == "pending_ring" {
                    d.pending_ring()
                } else {
                    d.pending_ring_for(s(o, "call"))
                };
                res = match req {
                    Some(r) => json!({"has": true, "call_id": st.call_mask(r.header("Call-ID")),
                                      "to_tag_set": !r.to_tag.is_empty()}),
                    None => json!({"has": false}),
                };
            }
            "open_relay" => {
                let r = drv.as_ref().unwrap().open_relay(&ctx).await;
                res = json!({"err": r.err().map(|x| x.message().to_string()).unwrap_or_default()});
            }
            "dial_bg" => {
                let d = drv.clone().unwrap();
                let (c, t) = (
                    ctx.clone(),
                    MediaTarget {
                        host: s(o, "host").to_string(),
                        port: o["port"].as_i64().unwrap_or(0),
                    },
                );
                dials.insert(
                    s(o, "call").to_string(),
                    tokio::spawn(async move {
                        d.dial(&c, t).await.map_err(|x| x.message().to_string())
                    }),
                );
            }
            "dial_result" => {
                let key = s(o, "call").to_string();
                if let Some(r) = dial_res.get(&key) {
                    res = r.clone();
                } else {
                    let h = dials.get_mut(&key).unwrap();
                    let r = match timeout(Duration::from_millis(o["ms"].as_u64().unwrap()), h).await
                    {
                        Ok(Ok(r)) => {
                            json!({"done": true, "err": e(&st, r.err().unwrap_or_default())})
                        }
                        Ok(Err(_)) => json!({"done": true, "err": "task panicked"}),
                        Err(_) => json!({"done": false}),
                    };
                    dial_res.insert(key, r.clone());
                    res = r;
                }
            }
            "stop" => {
                let r = drv.as_ref().unwrap().stop().await;
                let g = st.st.lock().unwrap();
                res = json!({"err": r.err().map(|x| x.message().to_string()).unwrap_or_default(),
                             "registers": g.regs, "deregisters": g.deregs});
            }
            "relay_start" => {
                let e2 = Endpoint::new("127.0.0.1", port as i64).await.unwrap();
                let (tx, rx) = tokio::sync::mpsc::channel(4);
                e2.handle("INVITE", move |r: IncomingRequest| {
                    let tx = tx.clone();
                    async move {
                        let _ = tx.send(r).await;
                    }
                });
                ep = Some(e2);
                caught = Some(rx);
            }
            "relay_dial" => {
                let r = Relay::new();
                let out = r
                    .dial_device(ep.as_ref().unwrap(), s(o, "a"), s(o, "b"), s(o, "host"))
                    .await;
                res = json!({"err": e(&st, out.err().unwrap_or_default()),
                             "call_id_set": !r.call_id().is_empty(), "audio_open": r.device_leg().is_some()});
                relay = Some(r);
            }
            "relay_answer" => {
                let req = timeout(Duration::from_secs(2), caught.as_mut().unwrap().recv())
                    .await
                    .expect("the ring never arrived")
                    .unwrap();
                let r = Relay::new();
                let out = r.answer_device(&req, s(o, "host")).await;
                let cid = r.call_id();
                res = json!({"err": e(&st, out.err().unwrap_or_default()),
                             "call_id": if cid.is_empty() { String::new() } else { st.call_mask(&cid) },
                             "audio_open": r.device_leg().is_some()});
                relay = Some(r);
            }
            "relay_audio" => res = relay_audio(&st, relay.as_ref().unwrap()).await,
            "relay_close" => {
                let r = relay.as_ref().unwrap().close().await;
                res = json!({"err": e(&st, r.err().unwrap_or_default())});
            }
            other => panic!("unknown op {other}"),
        }
        results.push(res);
    }
    if let Some(ep) = ep {
        let _ = ep.close();
    }
    if let Some(d) = drv {
        let _ = d.stop().await;
    }
    results
}

async fn relay_audio(st: &Station, r: &Relay) -> Value {
    let port = st.st.lock().unwrap().relay_port;
    assert_ne!(port, 0, "the relay's RTP port is unknown");
    let to: SocketAddr = format!("127.0.0.1:{port}").parse().unwrap();
    let leg = r.device_leg().expect("no device leg");
    let mut out = Map::new();
    let f = frame(0x30);
    let pkt = RtpPacket {
        payload_type: 96,
        sequence_number: 1,
        timestamp: 640,
        ssrc: 0x1122_3344,
        marker: false,
        payload: f.clone(),
    }
    .marshal();
    st.rtp.send_to(&pkt, to).await.unwrap();
    let got = timeout(Duration::from_secs(3), async {
        leg.output.lock().await.recv().await
    })
    .await;
    out.insert(
        "device_to_relay".into(),
        match got {
            Ok(Some(g)) => json!({"len": g.len(), "md5": md5hex(&g), "equal": g == f}),
            _ => json!("timeout"),
        },
    );
    let ours = frame(0x50);
    if timeout(Duration::from_secs(1), leg.input.send(ours.clone()))
        .await
        .is_err()
    {
        out.insert("relay_to_device".into(), json!("send blocked"));
        return Value::Object(out);
    }
    let mut buf = vec![0u8; 4096];
    let rd = timeout(Duration::from_secs(3), st.rtp.recv_from(&mut buf)).await;
    let v = match rd {
        Ok(Ok((n, _))) => match parse_rtp_packet(&buf[..n]) {
            Ok(p) => {
                json!({"pt": p.payload_type, "len": p.payload.len(), "md5": md5hex(&p.payload),
                            "equal": p.payload == ours})
            }
            Err(_) => json!("unparsable"),
        },
        _ => json!("timeout"),
    };
    out.insert("relay_to_device".into(), v);
    Value::Object(out)
}

fn apply_known(name: &str, results: &mut [Value], want: &[Value]) {
    for (scn, idx, go, rust) in KNOWN_DIFFERENCES {
        if *scn != name {
            continue;
        }
        let g = want[*idx]["err"].as_str().unwrap_or("");
        assert!(
            g.contains(go),
            "{name} op {idx}: Go recorded {g:?}, expected it to contain {go:?}"
        );
        assert_eq!(
            results[*idx]["err"].as_str().unwrap_or("?"),
            *rust,
            "{name} op {idx}"
        );
        results[*idx]["err"] = want[*idx]["err"].clone();
    }
}

#[tokio::test]
async fn golden_station_scenarios() {
    let g = load("dahua_station.json");
    let scs = g.as_array().unwrap();
    assert_eq!(scs.len(), 29);
    let only = std::env::var("DAHUA_ONLY").ok();
    let mut ran = 0;
    for sc in scs {
        let name = s(sc, "name");
        if let Some(o) = &only {
            if !name.contains(o.as_str()) {
                continue;
            }
        }
        ran += 1;
        let mut got = run_scenario(sc).await;
        let want = sc["results"].as_array().unwrap();
        apply_known(name, &mut got, want);
        assert_eq!(got.len(), want.len(), "{name}");
        for (i, (g1, w1)) in got.iter().zip(want).enumerate() {
            let op = &sc["ops"][i];
            assert_eq!(g1, w1, "{name}\n  op {i}: {}", op);
        }
    }
    assert!(ran > 0, "DAHUA_ONLY={only:?} matched no scenario");
    if only.is_none() {
        assert_eq!(ran, 29);
    }
}
