//! Golden vectors produced by the REAL Go `pkg/sip` (tools/gen_golden, see README.md) replayed
//! against the Rust port. Every vector must give identical output; the few documented
//! exceptions are listed in PARITY.md. (`lossy_err` in the parse/sdp JSON marks an input with
//! invalid UTF-8; since R2-112 the error text is compared for those too: `%q` writes `\xNN`.)

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Map, Value};
use tokio::net::UdpSocket;
use tokio::sync::mpsc;
use tokio::time::{sleep, timeout};
use transport_rs::cancel::CancelToken;
use transport_rs::sip::digest::{
    authorization_for_raw, build_authorization_header, digest_response, parse_digest_challenge,
    DigestChallenge,
};
use transport_rs::sip::{
    self, authorization_for, build_sdp, parse_request, parse_response, parse_rtp_packet, parse_sdp,
    verify_digest_authorization, DigestResult, Endpoint, IncomingRequest, JitterBuffer, NonceStore,
    Registrar, RtpPacket, SdpInfo,
};

fn load(name: &str) -> Value {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("tests/golden");
    p.push(name);
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    serde_json::from_str(&text).unwrap()
}

fn unhex(s: &str) -> Vec<u8> {
    assert!(s.len().is_multiple_of(2), "odd hex");
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

fn hexs(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

fn i(v: &Value, k: &str) -> i64 {
    v.get(k).and_then(Value::as_i64).unwrap_or(0)
}

fn b(v: &Value, k: &str) -> bool {
    v.get(k).and_then(Value::as_bool).unwrap_or(false)
}

fn headers_json(h: &HashMap<String, Vec<String>>) -> Value {
    let m: Map<String, Value> = h.iter().map(|(k, v)| (k.clone(), json!(v))).collect();
    Value::Object(m)
}

fn want_headers(v: &Value) -> Value {
    v.get("headers").cloned().unwrap_or_else(|| json!({}))
}

// ---- parse ----------------------------------------------------------------------------------

#[test]
fn golden_message_parsing() {
    let vecs = load("parse.json");
    let vecs = vecs.as_array().unwrap();
    assert_eq!(vecs.len(), 87, "corpus has {} inputs", vecs.len()); // exact (R2-107): a truncated golden file must fail
    for v in vecs {
        let name = s(v, "name");
        let input = unhex(s(v, "in_hex"));

        let want = &v["request"];
        match parse_request(&input) {
            Err(e) => {
                assert!(
                    !b(want, "ok"),
                    "request {name}: Rust failed ({e}), Go parsed"
                );
                assert_eq!(e.to_string(), s(want, "err"), "request {name}: error text");
            }
            Ok(r) => {
                assert!(
                    b(want, "ok"),
                    "request {name}: Rust parsed, Go failed: {}",
                    s(want, "err")
                );
                assert_eq!(r.method, s(want, "method"), "request {name}: method");
                assert_eq!(r.uri, s(want, "uri"), "request {name}: uri");
                assert_eq!(
                    headers_json(&r.headers),
                    want_headers(want),
                    "request {name}: headers"
                );
                assert_eq!(hexs(&r.body), s(want, "body_hex"), "request {name}: body");
            }
        }

        let want = &v["response"];
        match parse_response(&input) {
            Err(e) => {
                assert!(
                    !b(want, "ok"),
                    "response {name}: Rust failed ({e}), Go parsed"
                );
                assert_eq!(e.to_string(), s(want, "err"), "response {name}: error text");
            }
            Ok(r) => {
                assert!(
                    b(want, "ok"),
                    "response {name}: Rust parsed, Go failed: {}",
                    s(want, "err")
                );
                assert_eq!(r.status_code, i(want, "status"), "response {name}: status");
                assert_eq!(r.reason, s(want, "reason"), "response {name}: reason");
                assert_eq!(
                    headers_json(&r.headers),
                    want_headers(want),
                    "response {name}: headers"
                );
                assert_eq!(hexs(&r.body), s(want, "body_hex"), "response {name}: body");
            }
        }
    }
}

// ---- SDP -------------------------------------------------------------------------------------

#[test]
fn golden_sdp() {
    let doc = load("sdp.json");
    let parse = doc["parse"].as_array().unwrap();
    assert_eq!(parse.len(), 39); // exact (R2-107): a truncated golden file must fail
    for v in parse {
        let name = s(v, "name");
        let got = parse_sdp(&unhex(s(v, "in_hex")));
        match got {
            Err(e) => {
                assert!(!b(v, "ok"), "sdp {name}: Rust failed ({e}), Go parsed");
                assert_eq!(e.to_string(), s(v, "err"), "sdp {name}: error text");
            }
            Ok(info) => {
                assert!(
                    b(v, "ok"),
                    "sdp {name}: Rust parsed, Go failed: {}",
                    s(v, "err")
                );
                assert_eq!(
                    info,
                    SdpInfo {
                        host: s(v, "host").into(),
                        port: i(v, "port"),
                        payload_type: i(v, "payload_type")
                    },
                    "sdp {name}"
                );
            }
        }
    }
    // R2-87: a replay of an empty array would pass; the corpus holds 6 today
    assert_eq!(doc["build"].as_array().unwrap().len(), 6); // exact (R2-107): a truncated golden file must fail
    for v in doc["build"].as_array().unwrap() {
        assert_eq!(build_sdp(s(v, "host"), i(v, "port")), s(v, "out"));
    }
}

// ---- RTP -------------------------------------------------------------------------------------

#[test]
fn golden_rtp() {
    let doc = load("rtp.json");
    assert_eq!(sip::RTP_FRAME_BYTES as i64, i(&doc, "frame_bytes"));
    // R2-87: non-empty corpora (20 parse inputs, 6 marshal inputs today)
    assert_eq!(doc["parse"].as_array().unwrap().len(), 20); // exact (R2-107): a truncated golden file must fail
    assert_eq!(doc["marshal"].as_array().unwrap().len(), 6); // exact (R2-107): a truncated golden file must fail
    for v in doc["parse"].as_array().unwrap() {
        let name = s(v, "name");
        match parse_rtp_packet(&unhex(s(v, "in_hex"))) {
            Err(e) => {
                assert!(!b(v, "ok"), "rtp {name}: Rust failed ({e}), Go parsed");
                assert_eq!(e.to_string(), s(v, "err"), "rtp {name}");
            }
            Ok(p) => {
                assert!(b(v, "ok"), "rtp {name}: Rust parsed, Go failed");
                assert_eq!(
                    i64::from(p.payload_type),
                    i(v, "payload_type"),
                    "rtp {name}: pt"
                );
                assert_eq!(i64::from(p.sequence_number), i(v, "seq"), "rtp {name}: seq");
                assert_eq!(i64::from(p.timestamp), i(v, "timestamp"), "rtp {name}: ts");
                assert_eq!(i64::from(p.ssrc), i(v, "ssrc"), "rtp {name}: ssrc");
                assert_eq!(p.marker, b(v, "marker"), "rtp {name}: marker");
                assert_eq!(hexs(&p.payload), s(v, "payload_hex"), "rtp {name}: payload");
            }
        }
    }
    for v in doc["marshal"].as_array().unwrap() {
        let p = RtpPacket {
            payload_type: i(v, "payload_type") as u8,
            sequence_number: i(v, "seq") as u16,
            timestamp: i(v, "timestamp") as u32,
            ssrc: i(v, "ssrc") as u32,
            marker: b(v, "marker"),
            payload: unhex(s(v, "payload_hex")),
        };
        assert_eq!(hexs(&p.marshal()), s(v, "out_hex"), "{}", s(v, "name"));
    }
}

// ---- digest ----------------------------------------------------------------------------------

/// Replaces the value of `key="..."` with `<X>` so random cnonces and the responses that depend
/// on them can be compared structurally.
fn mask(h: &str) -> String {
    let mut out = h.to_string();
    for key in ["cnonce=\"", "response=\""] {
        if let Some(i) = out.find(key) {
            let start = i + key.len();
            let end = start + out[start..].find('"').unwrap();
            out.replace_range(start..end, "<X>");
        }
    }
    out
}

/// R2-4 / PARITY 420: Go's `splitDigestFields` ranges over the header as runes, so every byte that
/// is not UTF-8 reaches the hash and the echoed realm/nonce/opaque as U+FFFD. The Rust client keeps
/// the bytes (the station hashes its own bytes). `digest_raw.json` is `known_go_defect`: the
/// vectors are the real Go answer. Two things are pinned: Rust differs from Go on these inputs,
/// and Rust over the challenge with every invalid byte repaired to U+FFFD is byte-identical to
/// Go's answer, so U+FFFD is the only difference.
#[test]
fn golden_digest_raw_is_a_known_go_defect() {
    let doc = load("digest_raw.json");
    assert!(b(&doc, "known_go_defect"));
    let vecs = doc["vectors"].as_array().unwrap();
    assert_eq!(vecs.len(), 3); // exact (R2-107): a truncated golden file must fail
    for v in vecs {
        let name = s(v, "name");
        let challenge = unhex(s(v, "challenge_hex"));
        assert!(
            std::str::from_utf8(&challenge).is_err(),
            "{name}: the vector must not be UTF-8"
        );
        let go = unhex(s(v, "go_header_hex"));
        let ask = |c: &[u8]| {
            authorization_for_raw(
                c,
                s(v, "user").as_bytes(),
                s(v, "pass").as_bytes(),
                s(v, "method").as_bytes(),
                s(v, "uri").as_bytes(),
            )
            .unwrap()
        };
        assert_ne!(
            ask(&challenge),
            go,
            "{name}: Rust keeps the bytes, Go repairs them"
        );
        let repaired = String::from_utf8_lossy(&challenge).into_owned();
        assert_eq!(
            ask(repaired.as_bytes()),
            go,
            "{name}: Go's answer is Rust's over the repaired challenge"
        );
    }
}

#[test]
fn golden_digest() {
    let doc = load("digest.json");
    let known = doc["known_response"].as_object().unwrap();
    assert_eq!(known.len(), 1);
    // R2-86: the vector is recomputed, not only counted. Key: "user:realm:pass METHOD:uri nonce".
    for (key, want) in known {
        let (cred, rest) = key.split_once(' ').unwrap();
        let (req, nonce) = rest.split_once(' ').unwrap();
        let cred: Vec<&str> = cred.splitn(3, ':').collect();
        let (method, uri) = req.split_once(':').unwrap();
        let ch = DigestChallenge {
            realm: cred[1].into(),
            nonce: nonce.into(),
            opaque: String::new(),
            qop: String::new(),
            algo: "MD5".into(),
        };
        let got = digest_response(cred[0], cred[2], method, uri, &ch, "", 0);
        assert_eq!(&got, want.as_str().unwrap(), "known_response {key}");
    }

    let auth = doc["authorization_for"].as_array().unwrap();
    assert_eq!(auth.len(), 50); // exact (R2-107): a truncated golden file must fail
    for v in auth {
        let name = s(v, "name");
        let got = authorization_for(
            s(v, "challenge"),
            s(v, "user"),
            s(v, "pass"),
            s(v, "method"),
            s(v, "uri"),
        );
        match got {
            Err(e) => {
                assert!(!b(v, "ok"), "digest {name}: Rust failed ({e}), Go answered");
                assert_eq!(e.to_string(), s(v, "err"), "digest {name}: error text");
            }
            Ok(h) => {
                assert!(
                    b(v, "ok"),
                    "digest {name}: Rust answered, Go failed: {}",
                    s(v, "err")
                );
                let want = s(v, "header");
                if !b(v, "random_cnonce") {
                    assert_eq!(h, want, "digest {name}");
                } else {
                    // Structure must match with the random parts masked, and Go's own header
                    // must be reproduced exactly from the cnonce it drew.
                    assert_eq!(mask(&h), mask(want), "digest {name}: shape");
                    let i0 = want.find("cnonce=\"").unwrap() + 8;
                    let cnonce = &want[i0..i0 + want[i0..].find('"').unwrap()];
                    let ch = parse_digest_challenge(s(v, "challenge")).unwrap();
                    let resp = digest_response(
                        s(v, "user"),
                        s(v, "pass"),
                        s(v, "method"),
                        s(v, "uri"),
                        &ch,
                        cnonce,
                        1,
                    );
                    let rebuilt = build_authorization_header(
                        s(v, "user"),
                        s(v, "uri"),
                        &ch,
                        &resp,
                        cnonce,
                        1,
                    );
                    assert_eq!(rebuilt, want, "digest {name}: recomputed header");
                }
            }
        }
    }

    let ver = doc["verify"].as_array().unwrap();
    assert_eq!(ver.len(), 31); // exact (R2-107): a truncated golden file must fail
    for v in ver {
        let ns = NonceStore::new();
        if b(v, "nonce_issued") {
            ns.insert_for_test(s(v, "nonce"));
        }
        let got = verify_digest_authorization(
            &ns,
            s(v, "header"),
            s(v, "method"),
            s(v, "realm"),
            s(v, "password"),
        );
        let got = match got {
            DigestResult::Ok => "ok",
            DigestResult::Bad => "bad",
            DigestResult::Stale => "stale",
        };
        assert_eq!(
            got,
            s(v, "result"),
            "verify {}: {}",
            s(v, "name"),
            s(v, "header")
        );
    }
}

// ---- jitter ----------------------------------------------------------------------------------

async fn run_jitter(sc: &Value) -> Vec<Value> {
    let ms = |k: &str| Duration::from_millis(i(sc, k) as u64);
    let frame_len = i(sc, "frame_len") as usize;
    let jb = JitterBuffer::new(
        ms("ptime_ms"),
        ms("delay_ms"),
        frame_len,
        i(sc, "silence") as u8,
    );
    let mut res = Vec::new();
    for op in sc["ops"].as_array().unwrap() {
        let mut r = Map::new();
        match s(op, "op") {
            "push" => jb.push(i(op, "seq") as u16, vec![i(op, "fill") as u8; frame_len]),
            "recv" => {
                let t = match i(op, "timeout_ms") {
                    0 => 600,
                    t => t as u64,
                };
                // A closed buffer ends recv at once (Go would block): report it as a timeout.
                let out = match timeout(Duration::from_millis(t), jb.recv()).await {
                    Ok(Some(f)) => hexs(&f),
                    _ => "timeout".to_string(),
                };
                r.insert("out".into(), json!(out));
            }
            "sleep" => sleep(Duration::from_millis(i(op, "ms") as u64)).await,
            "drain" => {
                let mut outs = Vec::new();
                while let Some(f) = jb.try_recv() {
                    outs.push(hexs(&f));
                }
                if !outs.is_empty() {
                    r.insert("outs".into(), json!(outs));
                }
            }
            "close" => jb.close(),
            other => panic!("unknown op {other}"),
        }
        res.push(Value::Object(r));
    }
    res
}

#[tokio::test(start_paused = true)]
async fn golden_jitter() {
    let doc = load("jitter.json");
    let scs = doc.as_array().unwrap();
    assert_eq!(scs.len(), 29);
    for sc in scs {
        let got = run_jitter(sc).await;
        let want = sc["results"].as_array().unwrap();
        assert_eq!(got.len(), want.len());
        for (idx, (g, w)) in got.iter().zip(want).enumerate() {
            let w = if w.is_null() { json!({}) } else { w.clone() };
            assert_eq!(
                *g,
                w,
                "jitter scenario {:?}, op #{idx} {}",
                s(sc, "name"),
                sc["ops"][idx]
            );
        }
    }
}

// ---- wire scenarios --------------------------------------------------------------------------

/// Replaces `prefix` + exactly `n` lower-case hex digits (+ nothing else) with `prefix<repl>`.
fn sub_hex(s: &str, prefix: &str, n: usize, repl: &str) -> String {
    let mut out = String::new();
    let mut rest = s;
    while let Some(i) = rest.find(prefix) {
        let after = &rest[i + prefix.len()..];
        let hex_ok = after.len() >= n
            && after.as_bytes()[..n]
                .iter()
                .all(|c| matches!(c, b'0'..=b'9' | b'a'..=b'f'));
        if hex_ok {
            out.push_str(&rest[..i + prefix.len()]);
            out.push_str(repl);
            rest = &after[n..];
        } else {
            out.push_str(&rest[..i + prefix.len()]);
            rest = after;
        }
    }
    out.push_str(rest);
    out
}

fn norm(s: &str, resp: bool) -> String {
    let mut s = sub_hex(s, "branch=z9hG4bK", 16, "<B>");
    s = sub_hex(&s, "tag=", 16, "<T>");
    s = sub_hex(&s, "Call-ID: ", 16, "<C>");
    s = sub_hex(&s, "cnonce=\"", 16, "<N>");
    // 127.0.0.1:<digits>
    let mut out = String::new();
    let mut rest = s.as_str();
    while let Some(i) = rest.find("127.0.0.1:") {
        out.push_str(&rest[..i + 10]);
        let after = &rest[i + 10..];
        let digits = after.bytes().take_while(u8::is_ascii_digit).count();
        if digits > 0 {
            out.push_str("<P>");
        }
        rest = &after[digits..];
    }
    out.push_str(rest);
    let mut s = out;
    if resp {
        s = sub_hex(&s, "response=\"", 32, "<R>");
    }
    s
}

fn via_of(req: &str) -> String {
    for l in req.split("\r\n") {
        if l.starts_with("Via:") {
            return format!("{l}\r\n");
        }
    }
    String::new()
}

struct Peer {
    sock: Arc<UdpSocket>,
    got: Arc<Mutex<Vec<String>>>,
}

impl Peer {
    async fn new(rules: Vec<Value>) -> Peer {
        let sock = Arc::new(UdpSocket::bind("127.0.0.1:0").await.unwrap());
        let got: Arc<Mutex<Vec<String>>> = Arc::default();
        let (s2, g2) = (sock.clone(), got.clone());
        tokio::spawn(async move {
            let mut buf = vec![0u8; 65535];
            while let Ok((n, raddr)) = s2.recv_from(&mut buf).await {
                let req = String::from_utf8_lossy(&buf[..n]).into_owned();
                g2.lock().unwrap().push(req.clone());
                for r in &rules {
                    if !req.starts_with(s(r, "prefix")) {
                        continue;
                    }
                    if !s(r, "contains").is_empty() && !req.contains(s(r, "contains")) {
                        continue;
                    }
                    if !s(r, "not_contains").is_empty() && req.contains(s(r, "not_contains")) {
                        continue;
                    }
                    for rep in r["replies"].as_array().into_iter().flatten() {
                        let text = rep.as_str().unwrap().replace("{VIA}", &via_of(&req));
                        let _ = s2.send_to(text.as_bytes(), raddr).await;
                    }
                    break;
                }
            }
        });
        Peer { sock, got }
    }

    fn port(&self) -> u16 {
        self.sock.local_addr().unwrap().port()
    }

    fn received(&self, resp: bool) -> Vec<String> {
        self.got
            .lock()
            .unwrap()
            .iter()
            .map(|g| norm(g, resp))
            .collect()
    }
}

async fn settle() {
    sleep(Duration::from_millis(150)).await;
}

fn err_str<E: std::fmt::Display>(e: &Result<(), E>) -> String {
    e.as_ref()
        .err()
        .map(ToString::to_string)
        .unwrap_or_default()
}

async fn run_wire(sc: &Value) -> (Map<String, Value>, Vec<String>) {
    let rules = sc["rules"].as_array().cloned().unwrap_or_default();
    let peer = Peer::new(rules).await;
    let ep = Endpoint::new("127.0.0.1", i64::from(peer.port()))
        .await
        .unwrap();
    let mut res = Map::new();
    match s(sc, "kind") {
        "dial" => {
            let rang = Arc::new(Mutex::new(false));
            let r2 = rang.clone();
            let mut on_ring = move || *r2.lock().unwrap() = true;
            let call = sip::dial(
                &ep,
                s(sc, "our_user"),
                s(sc, "callee_user"),
                SdpInfo {
                    host: s(sc, "media_host").into(),
                    port: i(sc, "media_port"),
                    payload_type: 0,
                },
                Some(&mut on_ring),
            )
            .await;
            res.insert("ok".into(), json!(call.is_ok()));
            res.insert(
                "err".into(),
                json!(norm(
                    &call
                        .as_ref()
                        .err()
                        .map(ToString::to_string)
                        .unwrap_or_default(),
                    false
                )),
            );
            res.insert("rang".into(), json!(*rang.lock().unwrap()));
            if let Ok(call) = call {
                res.insert("remote_host".into(), json!(call.remote_sdp.host));
                res.insert("remote_port".into(), json!(call.remote_sdp.port));
                res.insert("remote_pt".into(), json!(call.remote_sdp.payload_type));
                res.insert(
                    "call_id".into(),
                    json!(norm(&format!("Call-ID: {}", call.call_id()), false)),
                );
                settle().await;
                if b(sc, "bye") {
                    res.insert("bye_err".into(), json!(err_str(&call.bye().await)));
                }
            }
            settle().await;
        }
        "register" => {
            let (tx, mut rx) = mpsc::unbounded_channel::<(bool, String)>();
            let mut r = Registrar::new(&ep, s(sc, "user"), s(sc, "pass"), i(sc, "expires"));
            r.on_state_change = Some(Box::new(move |ok, err| {
                let _ = tx.send((
                    ok,
                    norm(&err.map(ToString::to_string).unwrap_or_default(), false),
                ));
            }));
            let r = Arc::new(r);
            let ctx = CancelToken::new();
            let (r2, c2) = (r.clone(), ctx.clone());
            let mut done = tokio::spawn(async move { r2.run(&c2).await });
            let mut evs = Vec::new();
            let mut returned = true;
            let run_err = match timeout(Duration::from_millis(400), &mut done).await {
                Ok(r) => r.unwrap(),
                Err(_) => {
                    returned = false;
                    ctx.cancel();
                    match timeout(Duration::from_secs(4), &mut done).await {
                        Ok(r) => r.unwrap(),
                        Err(_) => Err(sip::Error::msg("HANG")),
                    }
                }
            };
            settle().await;
            while let Ok(e) = rx.try_recv() {
                evs.push(e);
            }
            res.insert(
                "events".into(),
                Value::Array(
                    evs.iter()
                        .map(|(ok, err)| json!({"ok": ok, "err": err}))
                        .collect(),
                ),
            );
            res.insert("returned_by_itself".into(), json!(returned));
            res.insert("run_err".into(), json!(norm(&err_str(&run_err), false)));
            res.insert(
                "busy".into(),
                json!(matches!(run_err, Err(sip::Error::BusyHere))),
            );
        }
        kind @ ("answer" | "respond") => {
            enum Got {
                Responded,
                Answered(Result<sip::Call, sip::Error>),
            }
            let (tx, mut rx) = mpsc::unbounded_channel::<(Got, String)>();
            let scn = sc.clone();
            let kind_s = kind.to_string();
            ep.handle(s(sc, "method"), move |req: IncomingRequest| {
                let (scn, tx, kind_s) = (scn.clone(), tx.clone(), kind_s.clone());
                async move {
                    let mut req = req;
                    if !s(&scn, "set_to_tag").is_empty() {
                        req.to_tag = s(&scn, "set_to_tag").to_string();
                    }
                    if kind_s == "respond" {
                        for r in scn["responds"].as_array().into_iter().flatten() {
                            let _ = req
                                .respond(
                                    i(r, "status"),
                                    s(r, "reason"),
                                    s(r, "to_tag"),
                                    s(r, "body"),
                                )
                                .await;
                        }
                        let _ = tx.send((Got::Responded, req.local_host()));
                        return;
                    }
                    if b(&scn, "ring") {
                        let _ = req.respond(180, "Ringing", &req.to_tag, "").await;
                    }
                    let call = sip::answer_incoming(
                        &req,
                        SdpInfo {
                            host: s(&scn, "media_host").into(),
                            port: i(&scn, "media_port"),
                            payload_type: 0,
                        },
                    )
                    .await;
                    let _ = tx.send((Got::Answered(call), String::new()));
                }
            });
            let inbound = s(sc, "inbound")
                .replace("{LOCAL}", ep.local_addr())
                .replace("{PEERPORT}", &peer.port().to_string());
            peer.sock
                .send_to(
                    inbound.as_bytes(),
                    ep.local_addr().parse::<std::net::SocketAddr>().unwrap(),
                )
                .await
                .unwrap();
            match timeout(Duration::from_secs(3), rx.recv()).await {
                Ok(Some((Got::Responded, lh))) => {
                    res.insert("local_host".into(), json!(lh));
                }
                Ok(Some((Got::Answered(r), _))) => {
                    res.insert("ok".into(), json!(r.is_ok()));
                    res.insert(
                        "err".into(),
                        json!(norm(
                            &r.as_ref()
                                .err()
                                .map(ToString::to_string)
                                .unwrap_or_default(),
                            false
                        )),
                    );
                    if let Ok(call) = r {
                        res.insert("remote_host".into(), json!(call.remote_sdp.host));
                        res.insert("remote_port".into(), json!(call.remote_sdp.port));
                        res.insert("remote_pt".into(), json!(call.remote_sdp.payload_type));
                        res.insert("call_id".into(), json!(call.call_id()));
                        settle().await;
                        if b(sc, "bye") {
                            res.insert("bye_err".into(), json!(err_str(&call.bye().await)));
                        }
                    }
                }
                _ => {
                    res.insert("handler".into(), json!("timeout"));
                }
            }
            settle().await;
        }
        other => panic!("unknown kind {other}"),
    }
    let received = peer.received(b(sc, "norm_response"));
    ep.close().unwrap();
    (res, received)
}

#[tokio::test]
async fn golden_wire_scenarios() {
    let doc = load("wire.json");
    let scs = doc.as_array().unwrap();
    assert_eq!(
        scs.len(),
        60,
        "the number of wire scenarios in the file changed"
    );
    let mut failures = Vec::new();
    for sc in scs {
        let name = s(sc, "name");
        let (res, received) = run_wire(sc).await;
        let want_recv: Vec<String> = sc["received"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_str().unwrap().to_string())
            .collect();
        if b(sc, "known_go_defect") {
            // Exempt from byte comparison (the Go output is a recorded defect, PARITY.md);
            // still replayed, and the ok/err outcome must agree.
            assert_eq!(
                res.get("ok"),
                sc["result"].get("ok"),
                "{name}: ok/err outcome"
            );
            continue;
        }
        if received != want_recv {
            failures.push(format!(
                "{name}: datagrams differ\n  go:   {want_recv:#?}\n  rust: {received:#?}"
            ));
        }
        let want_res = sc["result"].as_object().unwrap();
        if &res != want_res {
            failures.push(format!(
                "{name}: result differs\n  go:   {want_res:?}\n  rust: {res:?}"
            ));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
