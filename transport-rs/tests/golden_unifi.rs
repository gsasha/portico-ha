//! Golden vectors produced by the REAL Go `pkg/driver/unifiprotect` (tools/gen_golden/unifi_gen.go,
//! see README.md) replayed against the Rust port: `Configure` over a corpus, the events-frame
//! handler over 73 frames and 23 timing scenarios with the real ring timers, `nextBackoff`,
//! `net.SplitHostPort` and `url.PathEscape`; `readMessage` over scripted byte streams and
//! `dialWS` over scripted answers; the console API client against scripted HTTP answers; and 24
//! whole-driver scenarios against a fake console. The Rust fake console below does what the Go one
//! does; the replay requires every recorded result to be equal, bar the entries of the
//! `*_DIFFERENCES` tables (each is a numbered difference in PARITY.md).

mod common;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use common::unifi::*;
use serde_json::{json, Map, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::time::sleep;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::unifiprotect::api::{path_escape, ProtectApi};
use transport_rs::driver::unifiprotect::ring::next_backoff;
use transport_rs::driver::unifiprotect::tls::Tls;
use transport_rs::driver::unifiprotect::ws::{
    self, accept_for, pump, DialError, DialParams, PumpTiming, WsError,
};
use transport_rs::driver::unifiprotect::UnifiProtect;
use transport_rs::driver::{CallHangupper, Driver, TalkbackAnswerer};
use transport_rs::gostd::net::split_host_port;

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

fn ms(n: u64) -> Duration {
    Duration::from_millis(n)
}

/// The vectors' compact hex: space separated items, plain hex or `HH*N`.
fn unhx(t: &str) -> Vec<u8> {
    let mut out = Vec::new();
    for it in t.split_whitespace() {
        if let Some((h, n)) = it.split_once('*') {
            let b = u8::from_str_radix(h, 16).unwrap();
            out.extend(std::iter::repeat_n(b, n.parse().unwrap()));
        } else {
            out.extend(
                (0..it.len() / 2).map(|i| u8::from_str_radix(&it[2 * i..2 * i + 2], 16).unwrap()),
            );
        }
    }
    out
}

fn cfg_of(v: &Value) -> Map<String, Value> {
    v.as_object().cloned().unwrap_or_default()
}

// ---- unifi_driver.json ---------------------------------------------------------------------------

#[test]
fn golden_configure_backoff_and_std_helpers() {
    let g = load("unifi_driver.json");
    let mut n = 0;
    for v in g["configure"].as_array().unwrap() {
        let mut d = UnifiProtect::default();
        let r = d.configure(&cfg_of(&v["cfg"]));
        let name = s(v, "name");
        if name == "host query" {
            // Go accepts `h?x`; the port refuses a host that can change the request line (119)
            assert_eq!(s(v, "err"), "", "{name}");
            assert!(r.is_err(), "{name}");
            continue;
        }
        match r {
            Ok(()) => {
                assert_eq!(s(v, "err"), "", "{name}: Go refused, Rust accepted");
                assert_eq!(
                    d.capabilities().to_wire(),
                    s(v, "caps"),
                    "{name}: capabilities bytes"
                );
                assert_eq!(
                    d.capabilities().reports_availability,
                    v["ring"].as_bool().unwrap_or(false),
                    "{name}"
                );
            }
            Err(e) => assert_eq!(e.message(), s(v, "err"), "{name}"),
        }
        n += 1;
    }
    assert_eq!(
        UnifiProtect::default().capabilities().to_wire(),
        s(&g, "unconfigured_caps")
    );
    assert!(n >= 30);

    for v in g["backoff"].as_array().unwrap() {
        let (i, o, m) = (
            v["In"].as_u64().unwrap(),
            v["Out"].as_u64().unwrap(),
            v["Max"].as_u64().unwrap(),
        );
        assert_eq!(
            next_backoff(Duration::from_nanos(i), Duration::from_nanos(m)),
            Duration::from_nanos(o),
            "backoff {i} max {m}"
        );
    }
    for v in g["split_host_port"].as_array().unwrap() {
        let got = split_host_port(s(v, "in"))
            .ok()
            .map(|(h, p)| (h.to_string(), p.to_string()));
        if v["err"].as_bool().unwrap() {
            assert_eq!(got, None, "{:?}", s(v, "in"));
        } else {
            assert_eq!(
                got,
                Some((s(v, "host").to_string(), s(v, "port").to_string())),
                "{:?}",
                s(v, "in")
            );
        }
    }
    for v in g["path_escape"].as_array().unwrap() {
        // Go's PathEscape works on bytes of a string that may be invalid UTF-8; the vectors that
        // are not valid UTF-8 are compared on the lossy text only when both agree it is valid
        let input = s(v, "In");
        assert_eq!(path_escape(input), s(v, "Out"), "{input:?}");
    }
}

/// What a scenario's driver and console are, on the Rust side.
struct Rig {
    f: Option<FakeConsole>,
    d: Arc<UnifiProtect>,
    sink: Arc<RecSink>,
    host: String,
    started: bool,
}

fn mask(sink: &RecSink, host: &str) -> Vec<String> {
    sink.list()
        .into_iter()
        .map(|e| e.replace(host, "HOST"))
        .collect()
}

/// Runs one scenario of `unifi_driver.json` / `unifi_scen.json` and returns its results.
async fn run_scen(sc: &Value) -> Vec<Value> {
    let no_console = sc["no_console"].as_bool().unwrap_or(false);
    let tls = sc["tls"].as_bool().unwrap_or(false);
    let mut f = None;
    let mut host = "h:1".to_string();
    if !no_console {
        let c = FakeConsole::new(tls).await;
        host = c.host();
        if sc["closed"].as_bool().unwrap_or(false) {
            drop(c);
            sleep(ms(50)).await;
        } else {
            f = Some(c);
        }
    }
    let mut cfg =
        serde_json::json!({"nvr_host": host, "nvr_api_key": TEST_KEY, "camera_id": "cam1"});
    for (k, v) in cfg_of(&sc["cfg"]) {
        cfg[k] = v;
    }
    let mut d = UnifiProtect::default();
    d.set_scheme(if tls { "https" } else { "http" });
    d.set_timing(fast());
    d.configure(cfg.as_object().unwrap()).unwrap();
    let d = Arc::new(d);
    let sink = RecSink::new();
    d.start(&CancelToken::new(), sink.clone()).await.unwrap();
    let mut rig = Rig {
        f,
        d,
        sink,
        host,
        started: true,
    };
    let mut out = Vec::new();
    for op in sc["ops"].as_array().unwrap() {
        let r = match s(op, "op") {
            "wait_conn" => {
                let got = rig
                    .f
                    .as_ref()
                    .unwrap()
                    .connected_within(ms(op["ms"].as_u64().unwrap()))
                    .await;
                json!({"conn": got})
            }
            "send" => {
                rig.f.as_ref().unwrap().send(s(op, "text")).await;
                Value::Null
            }
            "send_hex" => {
                rig.f
                    .as_ref()
                    .unwrap()
                    .send_bytes_lossy(&unhx(s(op, "hex")))
                    .await;
                Value::Null
            }
            "drop" => {
                rig.f.as_ref().unwrap().drop_all();
                Value::Null
            }
            "status" => {
                rig.f
                    .as_ref()
                    .unwrap()
                    .set_status(op["n"].as_u64().unwrap_or(0) as u16);
                Value::Null
            }
            "no_pong" => {
                rig.f
                    .as_ref()
                    .unwrap()
                    .set_no_pong(op["b"].as_bool().unwrap_or(false));
                Value::Null
            }
            "sleep" => {
                sleep(ms(op["ms"].as_u64().unwrap())).await;
                Value::Null
            }
            "frame" => {
                rig.d.handle_frame_for_test(s(op, "text").as_bytes());
                Value::Null
            }
            "frame_hex" => {
                rig.d.handle_frame_for_test(&unhx(s(op, "hex")));
                Value::Null
            }
            "observe" => {
                let mut o = json!({"events": mask(&rig.sink, &rig.host)});
                if op["all"].as_bool().unwrap_or(false) {
                    if let Some(f) = &rig.f {
                        o["attempts"] = json!(f.attempt_count());
                    }
                }
                o
            }
            "hangup" => {
                let e = CallHangupper::hangup_call(&*rig.d, s(op, "call"))
                    .err()
                    .map(|e| e.to_string())
                    .unwrap_or_default();
                json!({"err": e.replace(&rig.host, "HOST")})
            }
            "ringing" => json!({"ringing": TalkbackAnswerer::ringing_call(&*rig.d, s(op, "call"))}),
            "stop" => {
                rig.started = false;
                let e = rig
                    .d
                    .stop()
                    .await
                    .err()
                    .map(|e| e.to_string())
                    .unwrap_or_default();
                json!({"err": e})
            }
            "start" => {
                rig.d
                    .start(&CancelToken::new(), rig.sink.clone())
                    .await
                    .unwrap();
                rig.started = true;
                Value::Null
            }
            other => panic!("unknown op {other}"),
        };
        out.push(r);
    }
    if rig.started {
        rig.d.stop().await.unwrap();
    }
    out
}

/// Entries whose Go answer differs on purpose: scenario name -> why (PARITY.md number).
const SCEN_DIFFERENCES: &[(&str, &str)] = &[
    // Go decodes a frame that is not UTF-8 (the bytes become U+FFFD); the port ignores it (110)
    ("raw 0", "110"),
    // an event id that is long or has odd characters is ignored (471, DEV-796 D10)
    ("frame 12", "471"),
    ("frame 13", "471"),
    ("frame 45", "471"),
    ("frame 46", "471"),
    ("frame 69", "471"),
];

/// `read` cases whose Go answer differs on purpose (470, DEV-796 D9): a control frame that is
/// not final or over 125 bytes, or a run of empty continuation frames, is a protocol error.
const GOLDEN_READ_DIFFERENCES: &[&str] = &[
    "ping 126",
    "ping 300",
    "ping 65535",
    "ping 65536",
    "ping 70000",
    "ping not final",
    "close not final",
    "random 0",
    "random 5",
    "random 18",
    "random 21",
    "random 22",
    "random 30",
    "random 34",
    "random 50",
    "random 51",
    "random 56",
    "random 57",
    "random 58",
    "random 65",
    "random 76",
    "random 78",
    "random 79",
    "random 83",
    "random 95",
    "random 99",
    "random 104",
    "random 123",
    "random 127",
    "random 128",
    "random 134",
    "random 135",
    "random 138",
    "random 144",
    "random 153",
];

async fn replay(file: &str, key: &str, only: Option<&str>) -> usize {
    let g = load(file);
    let list = g[key].as_array().unwrap();
    let mut n = 0;
    let mut bad: Vec<String> = Vec::new();
    for sc in list {
        let name = s(sc, "name");
        if let Some(p) = only {
            if !name.starts_with(p) {
                continue;
            }
        } else if file == "unifi_driver.json" && name.starts_with("timer:") {
            continue;
        }
        let got = run_scen(sc).await;
        let want = sc["results"].as_array().unwrap();
        let known = SCEN_DIFFERENCES.iter().find(|(n, _)| *n == name);
        if let Some((_, why)) = known {
            assert!(
                got.as_slice() != want.as_slice(),
                "{name} is listed as a known difference but now matches"
            );
            if *why == "471" {
                // what Rust does instead: nothing is reported and nothing rings (DEV-796 D10)
                for (i, r) in got.iter().enumerate().skip(1) {
                    assert!(
                        r["events"].as_array().is_none_or(|e| e.is_empty())
                            && r["ringing"] != Value::Bool(true),
                        "{name}: step {i} reported {r}"
                    );
                }
            }
            continue;
        }
        if got.as_slice() != want.as_slice() {
            bad.push(format!(
                "{name}:\n   got {}\n  want {}",
                Value::Array(got),
                Value::Array(want.clone())
            ));
        }
        n += 1;
    }
    assert!(
        bad.is_empty(),
        "{} of {n} scenarios differ:\n{}",
        bad.len(),
        bad.join("\n")
    );
    n
}

#[tokio::test]
async fn golden_event_frames() {
    // 70 frames and 3 raw byte strings (one of them a known difference) through the real handler, the sink events and RingingCall
    // right after (one of the raw ones is a known difference)
    let n = replay("unifi_driver.json", "events", None).await;
    // 73 scenarios, five of them the known differences of 471 (skipped, not counted)
    assert_eq!(n, 68, "{n}");
}

#[tokio::test]
async fn golden_ring_timers() {
    let n = replay("unifi_driver.json", "events", Some("timer:")).await;
    assert_eq!(n, 23, "{n}");
}

#[tokio::test]
async fn golden_driver_scenarios() {
    let n = replay("unifi_scen.json", "scenarios", None).await;
    assert!(n >= 24, "{n}");
}

// ---- unifi_ws.json ----------------------------------------------------------------------------------

#[derive(Debug, PartialEq, Eq)]
struct FrameRec {
    op: u8,
    form: u8,
    len: usize,
    fin: bool,
    payload: String,
    sum: String,
}

fn hexs(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// The frames a client wrote, as the generator records them (all masked).
fn parse_client(mut b: &[u8]) -> Vec<FrameRec> {
    let mut out = vec![];
    while b.len() >= 2 {
        let (fin, op) = (b[0] & 0x80 != 0, b[0] & 0x0f);
        let (mut n, mut h, mut form) = ((b[1] & 0x7f) as usize, 2usize, 7u8);
        if n == 126 {
            if b.len() < 4 {
                break;
            }
            n = u16::from_be_bytes([b[2], b[3]]) as usize;
            h = 4;
            form = 16;
        } else if n == 127 {
            if b.len() < 10 {
                break;
            }
            let mut x = [0u8; 8];
            x.copy_from_slice(&b[2..10]);
            n = u64::from_be_bytes(x) as usize;
            h = 10;
            form = 64;
        }
        let mut mask = [0u8; 4];
        if b[1] & 0x80 != 0 {
            if b.len() < h + 4 {
                break;
            }
            mask.copy_from_slice(&b[h..h + 4]);
            h += 4;
        }
        if b.len() < h + n {
            break;
        }
        let p: Vec<u8> = b[h..h + n]
            .iter()
            .enumerate()
            .map(|(i, x)| x ^ mask[i % 4])
            .collect();
        let (payload, sum) = if n <= 16 {
            (hexs(&p), String::new())
        } else {
            let d = aws_lc_rs::digest::digest(&aws_lc_rs::digest::SHA1_FOR_LEGACY_USE_ONLY, &p);
            (hexs(&p[..16]), hexs(d.as_ref()))
        };
        out.push(FrameRec {
            op,
            form,
            len: n,
            fin,
            payload,
            sum,
        });
        b = &b[h + n..];
    }
    out
}

fn frame_rec_of(v: &Value) -> FrameRec {
    FrameRec {
        op: v["op"].as_u64().unwrap() as u8,
        form: v["form"].as_u64().unwrap() as u8,
        len: v["len"].as_u64().unwrap() as usize,
        fin: v["fin"].as_bool().unwrap(),
        payload: s(v, "payload").to_string(),
        sum: s(v, "sum").to_string(),
    }
}

/// What a scripted server answers to an upgrade request: (the request head, the right accept) to bytes.
type Replier = Box<dyn Fn(&str, &str) -> Vec<u8> + Send>;

/// A server that answers the upgrade (`reply` = None: the right 101), writes `after`, and keeps
/// reading what the client writes until `linger` has passed (or `hold`: until `done`).
async fn script_server(
    reply: Option<Replier>,
    after: Vec<u8>,
    linger: Duration,
    hold: bool,
    done: CancelToken,
) -> (String, tokio::task::JoinHandle<(String, Vec<u8>)>) {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap().to_string();
    let h = tokio::spawn(async move {
        let (mut c, _) = l.accept().await.unwrap();
        let mut buf = Vec::new();
        let mut tmp = [0u8; 4096];
        let end = loop {
            if let Some(i) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                break i + 4;
            }
            let n = c.read(&mut tmp).await.unwrap_or(0);
            if n == 0 {
                return (String::new(), vec![]);
            }
            buf.extend_from_slice(&tmp[..n]);
        };
        let head = String::from_utf8_lossy(&buf[..end]).to_string();
        let key = header(&head, "Sec-WebSocket-Key").unwrap_or("").to_string();
        let acc = accept_for(&key);
        let rb = match &reply {
            None => format!("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n").into_bytes(),
            Some(f) => f(&head, &acc),
        };
        let mut w = rb;
        w.extend_from_slice(&after);
        let _ = c.write_all(&w).await;
        let mut rec: Vec<u8> = buf[end..].to_vec();
        let until = tokio::time::Instant::now() + linger;
        loop {
            tokio::select! {
                n = c.read(&mut tmp) => match n { Ok(0) | Err(_) => break, Ok(n) => rec.extend_from_slice(&tmp[..n]) },
                _ = tokio::time::sleep_until(until), if !hold => break,
                _ = done.cancelled(), if hold => break,
            }
        }
        (head, rec)
    });
    (addr, h)
}

fn class_of(e: &WsError) -> &'static str {
    match e {
        WsError::Closed => "closed",
        WsError::Protocol => "protocol",
        WsError::TooBig => "toobig",
        WsError::Eof => "eof",
        WsError::UnexpectedEof => "unexpected_eof",
        WsError::Timeout => "timeout",
        WsError::Io(_) => "io",
        WsError::Cancelled => "cancelled",
    }
}

#[tokio::test]
async fn golden_read_message() {
    let g = load("unifi_ws.json");
    let cases = g["read"].as_array().unwrap();
    let mut bad: Vec<String> = Vec::new();
    // 12 at a time, as the generator ran them
    let mut set = tokio::task::JoinSet::new();
    let sem = Arc::new(tokio::sync::Semaphore::new(12));
    for c in cases.iter().cloned() {
        let sem = sem.clone();
        set.spawn(async move {
            let _p = sem.acquire().await.unwrap();
            let name = s(&c, "name").to_string();
            let bytes = unhx(s(&c, "hex"));
            let hold = c["hold"].as_bool().unwrap_or(false);
            let done = CancelToken::new();
            let (addr, srv) = script_server(None, bytes, ms(400), hold, done.clone()).await;
            let tls = Tls::new(false);
            let conn = ws::dial(
                &CancelToken::new(),
                &DialParams {
                    scheme: "http",
                    host: &addr,
                    path: "/p",
                    api_key: TEST_KEY,
                    tls: &tls,
                    dial_timeout: Duration::from_secs(5),
                },
            )
            .await;
            let conn = match conn {
                Ok(c) => c,
                Err(e) => return (name, format!("dial: {e}")),
            };
            let msgs = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
            let m2 = msgs.clone();
            let err = pump(
                conn,
                &CancelToken::new(),
                PumpTiming {
                    ping_interval: Duration::from_secs(3600),
                    pong_wait: if hold {
                        ms(200)
                    } else {
                        Duration::from_secs(5)
                    },
                },
                move |m| m2.lock().unwrap().push(hexs(m)),
            )
            .await;
            done.cancel();
            let (_, rec) = srv.await.unwrap();
            let mut client = parse_client(&rec);
            // the pump closes the connection with a close frame after these; Go's readMessage
            // alone does not (the driver's `read` does, through the ping goroutine)
            if matches!(err, WsError::Protocol | WsError::TooBig | WsError::Timeout)
                && client.last().is_some_and(|f| f.op == 8)
            {
                client.pop();
            }
            let want_msgs: Vec<String> = c["msgs"]
                .as_array()
                .unwrap()
                .iter()
                .map(|m| hexs(&unhx(m.as_str().unwrap())))
                .collect();
            let want_client: Vec<FrameRec> = c["client"]
                .as_array()
                .unwrap()
                .iter()
                .map(frame_rec_of)
                .collect();
            let got_msgs = msgs.lock().unwrap().clone();
            let mut diff = String::new();
            if got_msgs != want_msgs {
                diff += &format!(" msgs differ ({} vs {})", got_msgs.len(), want_msgs.len());
            }
            if class_of(&err) != s(&c, "err") {
                diff += &format!(" err {} vs {}", class_of(&err), s(&c, "err"));
            }
            if client != want_client {
                diff += &format!(" client {client:?} vs {want_client:?}");
            }
            // a known difference must still differ, and anything else must match
            let known = GOLDEN_READ_DIFFERENCES.contains(&name.as_str());
            if known {
                // what Rust does instead of Go's answer: a protocol error, and no pong that
                // echoes more than a control frame may carry (DEV-796 D9)
                assert_eq!(class_of(&err), "protocol", "{name}");
                assert!(
                    client.iter().all(|f| f.op != 10 || f.len <= 125),
                    "{name}: {client:?}"
                );
            }
            let diff = match (known, diff.is_empty()) {
                (true, false) | (false, true) => String::new(),
                (true, true) => " listed as a known difference but now matches".to_string(),
                (false, false) => diff,
            };
            (name, diff)
        });
    }
    let mut n = 0;
    while let Some(r) = set.join_next().await {
        let (name, diff) = r.unwrap();
        n += 1;
        if !diff.is_empty() {
            bad.push(format!("{name}:{diff}"));
        }
    }
    assert!(
        bad.is_empty(),
        "{} of {n} differ:\n{}",
        bad.len(),
        bad.join("\n")
    );
    assert!(n >= 200, "{n}");
}

#[tokio::test]
async fn golden_upgrade_answers_and_what_the_client_sends() {
    let g = load("unifi_ws.json");
    let mut bad = vec![];
    let mut n = 0;
    for c in g["upgrade"].as_array().unwrap() {
        let name = s(c, "name").to_string();
        let tmpl = unhx(s(c, "reply"));
        let tmpl = String::from_utf8_lossy(&tmpl).to_string();
        let after: Vec<u8> = vec![];
        let reply = Box::new(move |_h: &str, acc: &str| tmpl.replace("<ACCEPT>", acc).into_bytes())
            as Replier;
        let done = CancelToken::new();
        let (addr, srv) = script_server(Some(reply), after, ms(100), false, done.clone()).await;
        let tls = Tls::new(false);
        let r = ws::dial(
            &CancelToken::new(),
            &DialParams {
                scheme: "http",
                host: &addr,
                path: "/p",
                api_key: TEST_KEY,
                tls: &tls,
                dial_timeout: Duration::from_secs(5),
            },
        )
        .await;
        let class = match r {
            Ok(_) => "ok".to_string(),
            Err(DialError::Status(c)) => format!("status:{c}"),
            Err(DialError::Cert) => "cert".to_string(),
            Err(DialError::Msg(m)) if m == "upgrade: bad Sec-WebSocket-Accept" => {
                "bad_accept".to_string()
            }
            Err(DialError::Msg(m)) if m.starts_with("upgrade: ") => "upgrade_error".to_string(),
            Err(e) => format!("other:{e}"),
        };
        done.cancel();
        let _ = srv.await;
        n += 1;
        if class != s(c, "class") {
            bad.push(format!("{name}: {class} vs {}", s(c, "class")));
        }
    }
    assert!(bad.is_empty(), "{n} cases:\n{}", bad.join("\n"));

    // the request, byte for byte but the key and the address
    let done = CancelToken::new();
    let reply = Box::new(|_: &str, _: &str| b"HTTP/1.1 400 X\r\n\r\n".to_vec()) as Replier;
    let (addr, srv) = script_server(Some(reply), vec![], ms(50), false, done.clone()).await;
    let tls = Tls::new(false);
    let _ = ws::dial(
        &CancelToken::new(),
        &DialParams {
            scheme: "http",
            host: &addr,
            path: "/proxy/protect/integration/v1/subscribe/events",
            api_key: TEST_KEY,
            tls: &tls,
            dial_timeout: Duration::from_secs(5),
        },
    )
    .await;
    let (head, _) = srv.await.unwrap();
    let masked: Vec<String> = head
        .replace(&addr, "HOST")
        .split("\r\n")
        .map(|l| {
            if l.starts_with("Sec-WebSocket-Key: ") {
                "Sec-WebSocket-Key: KEY".to_string()
            } else {
                l.to_string()
            }
        })
        .collect();
    assert_eq!(masked.join("\r\n"), s(&g, "request"));

    // what the client writes: ping() and writeFrame at every width
    let done = CancelToken::new();
    let (addr, srv) = script_server(None, vec![], ms(150), false, done.clone()).await;
    let mut conn = ws::dial(
        &CancelToken::new(),
        &DialParams {
            scheme: "http",
            host: &addr,
            path: "/p",
            api_key: TEST_KEY,
            tls: &tls,
            dial_timeout: Duration::from_secs(5),
        },
    )
    .await
    .unwrap();
    conn.write_frame(0x9, &[]).await.unwrap();
    for n in [0usize, 1, 125, 126, 65535, 65536, 70000] {
        conn.write_frame(0xA, &vec![b'w'; n]).await.unwrap();
    }
    conn.close().await;
    let (_, rec) = srv.await.unwrap();
    let got = parse_client(&rec);
    let want: Vec<FrameRec> = g["writes"]
        .as_array()
        .unwrap()
        .iter()
        .map(frame_rec_of)
        .collect();
    assert_eq!(got, want);
}

// ---- unifi_api.json -----------------------------------------------------------------------------------

/// Cases whose Go error TEXT is about the transport (net/http wording): compared by prefix.
const API_TEXT_DIFFERENCES: &[(&str, &str)] = &[
    ("rtsps: EOF at once", "net/http says `EOF`, wrapped in the URL error; the port's wording differs (108)"),
    ("rtsps: garbage instead of HTTP", "net/http: `malformed HTTP response \"garbage\"`; the port: `malformed HTTP response` (108)"),
];

#[tokio::test]
async fn golden_console_api_answers() {
    let g = load("unifi_api.json");
    let f = FakeProtect::new().await;
    let mut bad = vec![];
    let mut n = 0;
    for c in g["cases"].as_array().unwrap() {
        let name = s(c, "name");
        f.set_raw_reply(&unhx(s(c, "reply")));
        let before = f.reqs().len();
        let api = ProtectApi::new("http", &f.host(), TEST_KEY, true);
        let ctx = CancelToken::new();
        let mut diff = String::new();
        match s(c, "kind") {
            "rtsps" => {
                let r = api.rtsps_url(&ctx, s(c, "camera"), s(c, "quality")).await;
                match (r, c.get("ok").and_then(Value::as_str)) {
                    (Ok(u), Some(w)) if u == w => {}
                    (Err(e), None) => {
                        let want = s(c, "err").replace("HOST", &f.host());
                        let lenient = API_TEXT_DIFFERENCES.iter().any(|(x, _)| *x == name);
                        if lenient {
                            let pre = want.split(": ").next().unwrap_or("");
                            let pre2 = format!(
                                "POST /proxy/protect/integration/v1/cameras/{}/rtsps-stream",
                                path_escape(s(c, "camera"))
                            );
                            if !(e.starts_with(&pre2) || e.starts_with(pre)) {
                                diff = format!("{e:?} vs {want:?}");
                            }
                        } else if e != want {
                            diff = format!("{e:?} vs {want:?}");
                        }
                    }
                    (r, w) => diff = format!("{r:?} vs ok={w:?} err={:?}", s(c, "err")),
                }
            }
            _ => {
                let r = api.talkback_session(&ctx, s(c, "camera")).await;
                match (r, c.get("talk").filter(|t| !t.is_null())) {
                    (Ok(t), Some(w)) => {
                        if t.url != s(w, "url")
                            || t.codec != s(w, "codec")
                            || t.sampling_rate != w["sampling_rate"].as_i64().unwrap()
                            || t.bits_per_sample != w["bits_per_sample"].as_i64().unwrap()
                        {
                            diff = format!("{t:?} vs {w}");
                        }
                    }
                    (Err(e), None) => {
                        let want = s(c, "err").replace("HOST", &f.host());
                        if e != want {
                            diff = format!("{e:?} vs {want:?}");
                        }
                    }
                    (r, w) => diff = format!("{r:?} vs {w:?}"),
                }
            }
        }
        // the request, when the console saw one
        let req = &c["req"];
        if !s(req, "line").is_empty() && f.reqs().len() > before {
            let heads = f.heads();
            let head = heads.last().unwrap();
            let line = head.lines().next().unwrap_or("");
            if line != s(req, "line") {
                diff += &format!(" request line {line:?} vs {:?}", s(req, "line"));
            }
            for (k, v) in req["hdr"].as_object().unwrap() {
                let got = header(head, k);
                // Go sends no Content-Length on a body-less POST? (it sends `0`); both are recorded
                if got != v.as_str() {
                    diff += &format!(" header {k}: {got:?} vs {v}");
                }
            }
            let bodies = f.bodies();
            if bodies.last().map(String::as_str) != Some(s(req, "body")) {
                diff += &format!(" body {:?} vs {:?}", bodies.last(), s(req, "body"));
            }
        }
        n += 1;
        if !diff.is_empty() {
            bad.push(format!("{name}: {diff}"));
        }
    }
    assert!(
        bad.is_empty(),
        "{} of {n} differ:\n{}",
        bad.len(),
        bad.join("\n")
    );
    assert!(n >= 100, "{n}");
}
