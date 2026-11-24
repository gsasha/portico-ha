//! Replays `tests/golden/gss_server.json` and `tests/golden/gss_dial.json` (the outbound call of
//! dial.go; tools/gen_golden/dial_gen.go and dial_scen.go): scenarios recorded from the REAL Go gate_sip_server
//! (tools/gen_golden/server_gen.go, scenarios in server_scen.go) against the Rust server over UDP
//! loopback. Each scenario is a list of operations (station datagrams, calls on the server and
//! the driver, observations) and the result the Go server produced for every operation that
//! observes something; the Rust run must produce the same value for every one of them: every SIP
//! datagram the server sends (tags, branches, nonces and ports masked the way the generator masks
//! them), the RTP header fields, the counters, the sink events, return values and error texts.
//!
//! Timing: the server runs on real time, as it did when the vectors were recorded; windows are
//! generous and counters that depend on elapsed time are recorded as booleans.

mod common;

use std::collections::{HashMap, HashSet};
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use common::*;
use serde_json::{json, Value};
use tokio::net::UdpSocket;
use tokio::sync::mpsc;
use tokio::time::Instant;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::gatesipserver::{AudioClient, DialTimers, GateSipServer};
use transport_rs::driver::{
    AudioClaimer, CallAudio, CallDialer, CallHangupper, ConnectAnswerer, Driver, DriverError,
    MediaTarget, Sentinel,
};

fn lcg(mut seed: u32, n: usize) -> Vec<u8> {
    (0..n)
        .map(|_| {
            seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
            (seed >> 24) as u8
        })
        .collect()
}

fn is_alnum(c: u8) -> bool {
    c.is_ascii_alphanumeric()
}

/// `;tag=VALUE` matches: (start of the match, start of the value, end).
fn tag_matches(s: &str) -> Vec<(usize, usize, usize)> {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i + 5 <= b.len() {
        if &b[i..i + 5] == b";tag=" {
            let st = i + 5;
            let mut e = st;
            while e < b.len() && (is_alnum(b[e]) || matches!(b[e], b'.' | b'_' | b'-')) {
                e += 1;
            }
            if e > st {
                out.push((i, st, e));
                i = e;
                continue;
            }
        }
        i += 1;
    }
    out
}

/// Every `z9hG4bK` plus the alphanumeric run after it.
fn branch_tokens(s: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut rest = s;
    while let Some(i) = rest.find("z9hG4bK") {
        let after = &rest[i + 7..];
        let n = after.bytes().take_while(|&c| is_alnum(c)).count();
        if n > 0 {
            out.push(format!("z9hG4bK{}", &after[..n]));
        }
        rest = &after[n..];
    }
    out
}

fn mask_branches(s: &str, sent: &HashSet<String>) -> String {
    let mut b = String::new();
    let mut rest = s;
    while let Some(i) = rest.find("z9hG4bK") {
        let after = &rest[i + 7..];
        let n = after.bytes().take_while(|&c| is_alnum(c)).count();
        if n == 0 {
            b.push_str(&rest[..i + 7]);
            rest = after;
            continue;
        }
        b.push_str(&rest[..i]);
        let tok = format!("z9hG4bK{}", &after[..n]);
        if sent.contains(&tok) {
            b.push_str(&tok);
        } else {
            b.push_str("z9hG4bK<B>");
        }
        rest = &after[n..];
    }
    b.push_str(rest);
    b
}

fn go_str(v: &Value, k: &str) -> String {
    v.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}
fn go_i(v: &Value, k: &str) -> i64 {
    v.get(k).and_then(Value::as_i64).unwrap_or(0)
}

/// A `DialCall` running in the background (dial_gen.go `dialRun`).
struct DialRun {
    jh: tokio::task::JoinHandle<Result<(), DriverError>>,
    ctx: CancelToken,
    done: Option<Value>,
}

/// The first header called `name` in a raw SIP message, trimmed (`msgHdr`).
fn msg_hdr(raw: &str, name: &str) -> String {
    let head = raw.split("\r\n\r\n").next().unwrap_or("");
    for l in head.split("\r\n") {
        if let Some((k, v)) = l.split_once(':') {
            if k.eq_ignore_ascii_case(name) {
                return v.trim().to_string();
            }
        }
    }
    String::new()
}

/// The last raw datagram of `list` whose first line starts with `of ` (`lastOf`).
fn last_of(list: &[String], of: &str) -> String {
    list.iter()
        .rev()
        .find(|m| m.starts_with(&format!("{of} ")))
        .unwrap_or_else(|| panic!("no pending {of} datagram"))
        .clone()
}

/// `mutate`: "callid=X" changes the Call-ID, "cseq=N METHOD" the CSeq.
fn mutate(req: &str, mutation: &str) -> String {
    if mutation.is_empty() {
        return req.to_string();
    }
    let (k, v) = mutation.split_once('=').unwrap_or((mutation, ""));
    match k {
        "callid" => req.replacen(
            &format!("Call-ID: {}", msg_hdr(req, "Call-ID")),
            &format!("Call-ID: {v}"),
            1,
        ),
        "cseq" => req.replacen(
            &format!("CSeq: {}", msg_hdr(req, "CSeq")),
            &format!("CSeq: {v}"),
            1,
        ),
        _ => panic!("unknown mutation {mutation:?}"),
    }
}

struct Run {
    ord_b: bool,
    branch_n: HashMap<String, String>,
    pend: Vec<String>,
    opend: Vec<String>,
    dials: HashMap<String, DialRun>,
    h: Option<Harness>,
    outsider: Option<Station>,
    srv: SocketAddr,
    rtp_port: u16,
    sent_tags: HashSet<String>,
    sent_branches: HashSet<String>,
    tag_n: HashMap<String, String>,
    nonce_n: HashMap<String, String>,
    nonces: Vec<String>,
    cl: Option<Arc<AudioClient>>,
    frames: Option<mpsc::Receiver<Vec<u8>>>,
    audio: Option<Arc<dyn CallAudio>>,
    seq: u16,
    oseq: u16,
    last: Option<(u16, u32, u32)>,
}

impl Run {
    fn h(&self) -> &Harness {
        self.h.as_ref().expect("scenario has no server")
    }

    /// `maskBranchesOrd`: the branches the station did not send, numbered by first appearance.
    fn mask_branches_ord(&mut self, s: &str) -> String {
        let mut b = String::new();
        let mut rest = s;
        while let Some(i) = rest.find("z9hG4bK") {
            let after = &rest[i + 7..];
            let n = after.bytes().take_while(|&c| is_alnum(c)).count();
            if n == 0 {
                b.push_str(&rest[..i + 7]);
                rest = after;
                continue;
            }
            b.push_str(&rest[..i]);
            let tok = format!("z9hG4bK{}", &after[..n]);
            if self.sent_branches.contains(&tok) {
                b.push_str(&tok);
            } else {
                let k = self.branch_n.len() + 1;
                let ord = self
                    .branch_n
                    .entry(tok)
                    .or_insert_with(|| format!("<B{k}>"))
                    .clone();
                b.push_str("z9hG4bK");
                b.push_str(&ord);
            }
            rest = &after[n..];
        }
        b.push_str(rest);
        b
    }

    fn mask(&mut self, raw: &str) -> String {
        let s = if self.ord_b {
            self.mask_branches_ord(raw)
        } else {
            mask_branches(raw, &self.sent_branches)
        };
        let mut b = String::new();
        let mut last = 0;
        for (m0, vs, ve) in tag_matches(&s) {
            b.push_str(&s[last..m0]);
            let tag = &s[vs..ve];
            if self.sent_tags.contains(tag) {
                b.push_str(&s[m0..ve]);
            } else {
                let n = self.tag_n.len() + 1;
                let ord = self
                    .tag_n
                    .entry(tag.to_string())
                    .or_insert_with(|| format!("<T{n}>"))
                    .clone();
                b.push_str(";tag=");
                b.push_str(&ord);
            }
            last = ve;
        }
        b.push_str(&s[last..]);
        let mut s = b;
        // nonce="..."
        let mut nb = String::new();
        loop {
            let Some(i) = s.find("nonce=\"") else {
                nb.push_str(&s);
                break;
            };
            nb.push_str(&s[..i + 7]);
            let rest = s[i + 7..].to_string();
            let Some(j) = rest.find('"') else {
                nb.push_str(&rest);
                break;
            };
            let nonce = rest[..j].to_string();
            let n = self.nonce_n.len() + 1;
            if !self.nonce_n.contains_key(&nonce) {
                self.nonce_n.insert(nonce.clone(), format!("<N{n}>"));
                self.nonces.push(nonce.clone());
            }
            nb.push_str(&self.nonce_n[&nonce]);
            s = rest[j..].to_string();
        }
        let (st, out) = (
            self.h().st.conn.local_addr().unwrap().to_string(),
            self.outsider
                .as_ref()
                .unwrap()
                .conn
                .local_addr()
                .unwrap()
                .to_string(),
        );
        nb.replace(&st, "<ST>")
            .replace(&out, "<OUT>")
            .replace(&self.srv.to_string(), "<SRV>")
            .replace(&format!("m=audio {} ", self.rtp_port), "m=audio <RTP> ")
    }

    fn note(&mut self, msg: &str) {
        for (_, vs, ve) in tag_matches(msg) {
            self.sent_tags.insert(msg[vs..ve].to_string());
        }
        for b in branch_tokens(msg) {
            self.sent_branches.insert(b);
        }
    }

    fn station(&self, from: &str) -> &Station {
        if from == "outsider" {
            self.outsider.as_ref().unwrap()
        } else {
            &self.h().st
        }
    }

    fn sdp(&self, from: &str, kind: &str) -> String {
        let st = self.station(from);
        let me = st.me.ip().to_string();
        let head = format!("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=x\r\nc=IN IP4 {me}\r\nt=0 0\r\n");
        match kind {
            "third-party-c" => {
                st.sdp("narrow")
                    .replacen(&format!("c=IN IP4 {me}"), "c=IN IP4 192.168.2.9", 1)
            }
            "garbage-c" => {
                st.sdp("narrow")
                    .replacen(&format!("c=IN IP4 {me}"), "c=IN IP4 999.1.1.1", 1)
            }
            "port-70000" => format!("{head}m=audio 70000 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\n"),
            "port-huge" => {
                format!("{head}m=audio 99999999999999999999 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\n")
            }
            "wide-c2" => {
                st.sdp("wide")
                    .replacen(&format!("c=IN IP4 {me}"), "c=IN IP4 127.0.0.2", 1)
            }
            "wide-port0" => {
                st.sdp("wide")
                    .replacen(&format!("m=audio {}", st.rtp_port()), "m=audio 0", 1)
            }
            "no-media" => head,
            "none" => String::new(),
            other => st.sdp(other),
        }
    }

    async fn send_from(&mut self, from: &str, text: &str) {
        self.note(text);
        self.station(from).send(text).await;
    }

    async fn register(&mut self, o: &Value) {
        let from = go_str(o, "from");
        let auth_kind = go_str(o, "auth");
        let nonce = match auth_kind.as_str() {
            "ok" | "bad" => self.nonces.last().cloned().unwrap_or_default(),
            "first" => self.nonces.first().cloned().unwrap_or_default(),
            "forged" => "forgednonce0123456789abcdef012345".to_string(),
            _ => String::new(),
        };
        let pass = if auth_kind == "bad" {
            "wrongpassw"
        } else {
            "s3cretpass"
        };
        let auth = match auth_kind.as_str() {
            "" => String::new(),
            "nofields" => "Digest username=\"8001\"".to_string(),
            _ => digest_for("8001", pass, "VDP", &nonce),
        };
        let text = self.station(&from).register_text(&auth);
        self.send_from(&from, &text).await;
    }

    async fn recv(&mut self, o: &Value) -> Value {
        let from = go_str(o, "from");
        let ms = go_i(o, "ms") as u64;
        let msgs = self.station(&from).collect(Duration::from_millis(ms)).await;
        for m in &msgs {
            if from == "outsider" {
                self.opend.push(m.raw.clone());
            } else {
                self.pend.push(m.raw.clone());
            }
        }
        let masked: Vec<Value> = msgs
            .iter()
            .map(|m| Value::String(self.mask(&m.raw)))
            .collect();
        Value::Array(masked)
    }

    fn payload_kind(p: &[u8]) -> serde_json::Map<String, Value> {
        let all_eq = |b: u8| !p.is_empty() && p.iter().all(|&x| x == b);
        let mut m = serde_json::Map::new();
        if all_eq(0xD5) {
            m.insert("kind".into(), json!("alaw-silence"));
        } else if all_eq(0) {
            m.insert("kind".into(), json!("zero"));
        } else {
            m.insert("kind".into(), json!("data"));
            m.insert("md5".into(), json!(md5hex_bytes(p)));
        }
        m.insert("len".into(), json!(p.len()));
        m
    }

    fn rtp_record(&mut self, h: &[u8], p: &[u8]) -> Value {
        let seq = u16::from_be_bytes([h[2], h[3]]);
        let ts = u32::from_be_bytes([h[4], h[5], h[6], h[7]]);
        let ssrc = u32::from_be_bytes([h[8], h[9], h[10], h[11]]);
        let mut rec = Self::payload_kind(p);
        rec.insert("b0".into(), json!(h[0]));
        rec.insert("pt".into(), json!(h[1]));
        match self.last {
            Some((_, _, s)) if s == ssrc => {
                let (ls, lt, _) = self.last.unwrap();
                rec.insert("seq_d".into(), json!(seq.wrapping_sub(ls)));
                rec.insert("ts_d".into(), json!(ts.wrapping_sub(lt)));
            }
            _ => {
                rec.insert("new_ssrc".into(), json!(true));
                rec.insert("seq_d".into(), json!(0));
                rec.insert("ts_d".into(), json!(0));
            }
        }
        self.last = Some((seq, ts, ssrc));
        Value::Object(rec)
    }

    fn stats(&self) -> Value {
        let sn = self.h().s.snapshot();
        let (st, out, srv) = (
            self.h().st.conn.local_addr().unwrap().to_string(),
            self.outsider
                .as_ref()
                .unwrap()
                .conn
                .local_addr()
                .unwrap()
                .to_string(),
            self.srv.to_string(),
        );
        let from = sn
            .registered_from
            .replace(&st, "<ST>")
            .replace(&out, "<OUT>")
            .replace(&srv, "<SRV>");
        json!({
            "registered": sn.registered, "registered_from": from, "digest_ok": sn.digest_ok,
            "digest_mismatch": sn.digest_mismatch, "calls": sn.calls, "answered": sn.answered,
            "rtp_in": sn.rtp_in, "rtp_out_positive": sn.rtp_out > 0, "rtp_rejected": sn.rtp_rejected,
            "last_err": sn.last_err, "ringing": sn.ringing, "in_call": sn.in_call,
            "dialog_confirmed": sn.dialog_confirmed, "app_attached": sn.app_attached,
            "frames_to_app": sn.frames_to_app, "frames_from_app": sn.frames_from_app,
            "dropped_up_positive": sn.dropped_up > 0, "dropped_down": sn.dropped_down,
            "dropped": sn.dropped, "dials": sn.dials, "dialing": sn.dialing,
        })
    }

    fn frame_rec(f: &[u8]) -> Value {
        json!({"len": f.len(), "md5": md5hex_bytes(f)})
    }

    async fn exec(&mut self, o: &Value) -> Value {
        let op = go_str(o, "op");
        let call = go_str(o, "call");
        let from = go_str(o, "from");
        let ctx = CancelToken::new();
        match op.as_str() {
            "register" => self.register(o).await,
            "invite" => {
                let sdp = self.sdp(&from, &go_str(o, "sdp"));
                let cseq = match go_i(o, "cseq") {
                    0 => 1,
                    n => n as u32,
                };
                let text = self.station(&from).invite_text(&call, cseq, &sdp);
                self.send_from(&from, &text).await;
            }
            "sip" => {
                let method = go_str(o, "method");
                let cseq = match go_i(o, "cseq") {
                    0 if method == "BYE" => 2,
                    0 => 1,
                    n => n as u32,
                };
                let call = if call.is_empty() && method == "OPTIONS" {
                    "opt".to_string()
                } else {
                    call
                };
                let text = self.station(&from).simple_text(&method, &call, cseq);
                self.send_from(&from, &text).await;
            }
            "raw" => {
                let text = go_str(o, "text");
                self.send_from("", &text).await;
            }
            "rtp" => {
                let pt = match go_i(o, "pt") {
                    0 => 8,
                    n => n as u8,
                };
                let size = match go_i(o, "size") {
                    0 => 160,
                    n => n as usize,
                };
                let seed = go_i(o, "seed") as u32;
                let empty = go_str(o, "what") == "empty";
                for i in 0..go_i(o, "n") {
                    let seq = if from == "outsider" {
                        self.oseq = self.oseq.wrapping_add(1);
                        self.oseq
                    } else {
                        self.seq = self.seq.wrapping_add(1);
                        self.seq
                    };
                    let payload = if empty {
                        Vec::new()
                    } else if seed != 0 {
                        lcg(seed + i as u32, size)
                    } else {
                        vec![0u8; size]
                    };
                    let pkt = rtp_packet(pt, seq, u32::from(seq) * 160, &payload);
                    let dst = SocketAddr::new("127.0.0.1".parse().unwrap(), self.rtp_port);
                    self.station(&from).rtp.send_to(&pkt, dst).await.unwrap();
                }
            }
            "sleep" => sleep_ms(go_i(o, "ms") as u64).await,
            "nonce_advance" => self
                .h()
                .s
                .advance_nonce_clock(Duration::from_secs(go_i(o, "secs") as u64)),
            "reg_ttl" => self.h().s.set_reg_ttl(ms(go_i(o, "ms") as u64)),
            "recv" => return self.recv(o).await,
            "rtp_drain" => while self.h().st.read_rtp(ms(30)).await.is_some() {},
            "rtp_read" => {
                assert!(
                    o.get("count").is_none(),
                    "count mode is not used by any scenario"
                );
                let (n, within) = (go_i(o, "n") as usize, go_i(o, "ms") as u64);
                let end = Instant::now() + ms(within);
                let mut recs = Vec::new();
                while recs.len() < n && Instant::now() < end {
                    let Some((h, p)) = self.h().st.read_rtp(end - Instant::now()).await else {
                        break;
                    };
                    recs.push(self.rtp_record(&h, &p));
                }
                return Value::Array(recs);
            }
            "rtp_find_data" => {
                let end = Instant::now() + ms(go_i(o, "ms") as u64);
                while Instant::now() < end {
                    let Some((h, p)) = self.h().st.read_rtp(end - Instant::now()).await else {
                        break;
                    };
                    let k = Self::payload_kind(&p);
                    if k["kind"] == "data" {
                        return json!({"found": true, "pt": h[1], "len": k["len"], "md5": k["md5"]});
                    }
                }
                return json!({"found": false});
            }
            "snapshot" => return self.stats(),
            "events" => {
                sleep_ms(go_i(o, "ms") as u64).await;
                // A REGISTER in a later wall-clock second republishes the capabilities with only
                // LastRegisterAt (DEV-518) moved, which these scenarios mask; whether two REGISTERs
                // straddle a second boundary is timing, so adjacent identical `caps:` entries are one.
                let mut ev = self.h().sink.events();
                ev.dedup_by(|b, a| a.starts_with("caps:") && a == b);
                return json!(ev);
            }
            "attach" => match self.h().s.attach() {
                Err(e) => {
                    return json!({"err": e.to_string(), "no_such_call": false, "already_answered": false, "not_supported": false})
                }
                Ok(cl) => {
                    self.frames = cl.take_frames();
                    let rec = json!({"ok": true, "rate": cl.rate, "frame_bytes": cl.frame_bytes});
                    self.cl = Some(cl);
                    return rec;
                }
            },
            "answer_current" => {
                return err_rec_server(self.h().s.answer_current().err().map(|e| e.to_string()))
            }
            "hangup" => return json!({"bool": self.h().s.hangup()}),
            "hangup_answered" | "drv_hangup_call" => {
                return err_rec(self.h().d.hangup_call(&call).err());
            }
            "client" => {
                let cl = self.cl.clone().expect("no client");
                match go_str(o, "what").as_str() {
                    "goodbye" => cl.goodbye(),
                    "detach" => cl.detach(),
                    "reason" => return json!({"str": cl.reason()}),
                    "send" => {
                        let (mut ok, mut bad) = (0, 0);
                        for i in 0..go_i(o, "n") {
                            let f =
                                lcg(go_i(o, "seed") as u32 + i as u32, go_i(o, "size") as usize);
                            if cl.send(&f).is_ok() {
                                ok += 1;
                            } else {
                                bad += 1;
                            }
                        }
                        return json!({"sent": ok, "errs": bad});
                    }
                    w => panic!("unknown client op {w:?}"),
                }
            }
            "frames" => {
                let n = go_i(o, "n") as usize;
                let end = Instant::now() + ms(go_i(o, "ms") as u64);
                let rx = self.frames.as_mut().expect("no frames");
                let mut recs = Vec::new();
                while recs.len() < n {
                    match tokio::time::timeout_at(end, rx.recv()).await {
                        Ok(Some(f)) => recs.push(Self::frame_rec(&f)),
                        Ok(None) => return json!({"frames": recs, "closed": true}),
                        Err(_) => return json!({"frames": recs, "closed": false}),
                    }
                }
                return json!({"frames": recs, "closed": false});
            }
            "frames_closed" => {
                let end = Instant::now() + ms(go_i(o, "ms") as u64);
                let rx = self.frames.as_mut().expect("no frames");
                loop {
                    match tokio::time::timeout_at(end, rx.recv()).await {
                        Ok(Some(_)) => {}
                        Ok(None) => return json!({"closed": true}),
                        Err(_) => return json!({"closed": false}),
                    }
                }
            }
            "drv_answer_with_audio" => {
                let r = self.h().d.answer_with_audio(&ctx, &call).await;
                match r {
                    Ok(a) => {
                        self.audio = Some(a);
                        return json!({"ok": true, "audio": true});
                    }
                    Err(e) => {
                        let mut rec = err_rec(Some(e));
                        rec["audio"] = json!(false);
                        return rec;
                    }
                }
            }
            "drv_answer" => {
                let t = MediaTarget {
                    host: go_str(o, "host"),
                    port: go_i(o, "port"),
                };
                return err_rec(self.h().d.answer(&ctx, t).await.err());
            }
            "drv_ringing_call" => {
                return json!({"bool": ConnectAnswerer::ringing_call(&*self.h().d, &call)})
            }
            "drv_claimed_audio" => {
                return json!({"bool": self.h().d.claimed_audio(&call).is_some()})
            }
            "drv_caps" => {
                // The lapse scenario reads the capabilities at exactly the registration's
                // lifetime (250 + 150 ms of waiting against a 400 ms lifetime, counted from
                // the REGISTER being sent while the server stamps it when it is processed), so
                // whether the lifetime has run out by then is a coin toss for a harness that
                // is quick. A few ms of slack make it a certainty; events published meanwhile
                // are read by the next `events` op, as in the golden.
                sleep_ms(25).await;
                // LastRegisterAt (DEV-518) is the wall clock at the REGISTER: masked, as tags and ports are.
                let wire = self.h().d.capabilities().to_wire();
                let wire = match wire.find("\"LastRegisterAt\":") {
                    Some(i) => {
                        let v = i + "\"LastRegisterAt\":".len();
                        let n = wire[v..].bytes().take_while(u8::is_ascii_digit).count();
                        format!("{}\"<unix>\"{}", &wire[..v], &wire[v + n..])
                    }
                    None => wire,
                };
                return json!({"wire": wire});
            }
            "audio" => {
                let a = self.audio.clone().expect("no audio");
                match go_str(o, "what").as_str() {
                    "attach" => a.attach(),
                    "detach" => a.detach(),
                    "in" => {
                        let f = lcg(go_i(o, "seed") as u32, go_i(o, "size") as usize);
                        tokio::time::timeout(ms(1000), a.input().send(f))
                            .await
                            .expect("In blocked")
                            .unwrap();
                    }
                    "out" => {
                        let out = a.output();
                        let mut rx = out.lock().await;
                        return match tokio::time::timeout(ms(go_i(o, "ms") as u64), rx.recv()).await
                        {
                            Ok(Some(f)) => Self::frame_rec(&f),
                            Ok(None) => json!({"closed": true}),
                            Err(_) => json!({"none": true}),
                        };
                    }
                    "out_closed" => {
                        let out = a.output();
                        let mut rx = out.lock().await;
                        let end = Instant::now() + ms(go_i(o, "ms") as u64);
                        loop {
                            match tokio::time::timeout_at(end, rx.recv()).await {
                                Ok(Some(_)) => {}
                                Ok(None) => return json!({"closed": true}),
                                Err(_) => return json!({"closed": false}),
                            }
                        }
                    }
                    w => panic!("unknown audio op {w:?}"),
                }
            }
            "stop" => return err_rec(self.h().d.stop().await.err()),
            "server_after_stop" => return json!({"server_nil": self.h().d.real_server().is_none()}),
            "configure_check" => {
                let mut d = GateSipServer::default();
                let cfg = json!({"sip_listen": "127.0.0.1:0", "password": go_str(o, "pwd")});
                let err = d
                    .configure(cfg.as_object().unwrap())
                    .err()
                    .map(|e| e.message().to_string());
                return json!({"err": err.unwrap_or_default()});
            }
            "dial" => {
                let ctx = CancelToken::new();
                let (d, c2, id) = (self.h().d.clone(), ctx.clone(), call.clone());
                let jh =
                    tokio::spawn(
                        async move { d.dial_call(&c2, &id, MediaTarget::default()).await },
                    );
                self.dials.insert(
                    call,
                    DialRun {
                        jh,
                        ctx,
                        done: None,
                    },
                );
            }
            "dial_cancel" => self.dials[&call].ctx.cancel(),
            "dial_result" => {
                let within = ms(go_i(o, "ms") as u64);
                let dr = self.dials.get_mut(&call).expect("no such dial");
                if dr.done.is_none() {
                    match tokio::time::timeout(within, &mut dr.jh).await {
                        Ok(r) => dr.done = Some(err_rec(r.expect("DialCall panicked").err())),
                        Err(_) => return json!({"pending": true}),
                    }
                }
                return dr.done.clone().unwrap();
            }
            "dial_timers" => {
                let opt = |k: &str| (go_i(o, k) > 0).then(|| ms(go_i(o, k) as u64));
                self.h().s.set_dial_timers(DialTimers {
                    ring: opt("ring"),
                    cancel_wait: opt("cancel_wait"),
                    cap: opt("cap"),
                    attach: opt("att"),
                });
            }
            "reply" => {
                let pend = if from == "outsider" {
                    &self.opend
                } else {
                    &self.pend
                };
                let req = mutate(&last_of(pend, &go_str(o, "of")), &go_str(o, "mut"));
                let host = go_str(o, "host");
                let mut to = msg_hdr(&req, "To");
                let tag = go_str(o, "tag");
                if !tag.is_empty() && !to.contains(";tag=") {
                    to.push_str(&format!(";tag={tag}"));
                }
                let kind = go_str(o, "sdp");
                let body = if kind.is_empty() {
                    String::new()
                } else {
                    self.sdp(&host, &kind)
                };
                let mut l = vec![
                    format!("SIP/2.0 {} {}", go_i(o, "code"), go_str(o, "reason")),
                    format!("Via: {}", msg_hdr(&req, "Via")),
                    format!("From: {}", msg_hdr(&req, "From")),
                    format!("To: {to}"),
                    format!("Call-ID: {}", msg_hdr(&req, "Call-ID")),
                    format!("CSeq: {}", msg_hdr(&req, "CSeq")),
                    format!("Contact: <sip:9901@{}>", self.station(&host).me),
                ];
                if let Some(h) = o.get("hdr").and_then(Value::as_array) {
                    l.extend(h.iter().map(|x| x.as_str().unwrap().to_string()));
                }
                if !body.is_empty() {
                    l.push("Content-Type: application/sdp".to_string());
                }
                l.push(format!("Content-Length: {}", body.len()));
                // not noted: the tags and branches in a response are the server's
                self.station(&host)
                    .send(&format!("{}\r\n\r\n{body}", l.join("\r\n")))
                    .await;
            }
            "st_dialog" => {
                let inv = last_of(&self.pend, "INVITE");
                let cseq = match go_i(o, "cseq") {
                    0 => 7,
                    n => n,
                };
                let method = go_str(o, "method");
                let msg = format!(
                    "{method} sip:8001@127.0.0.1:{} SIP/2.0\r\nVia: SIP/2.0/UDP {};branch=z9hG4bKstdlg\r\n\
                     From: <sip:9901@127.0.0.1>;tag=st1\r\nTo: {}\r\nCall-ID: {}\r\nCSeq: {cseq} {method}\r\nContent-Length: 0\r\n\r\n",
                    self.srv.port(),
                    self.h().st.me,
                    msg_hdr(&inv, "From"),
                    msg_hdr(&inv, "Call-ID"),
                );
                self.h().st.send(&msg).await;
            }
            "drv_claim" => {
                let a = self.h().d.claimed_audio(&call);
                let ok = a.is_some();
                if a.is_some() {
                    self.audio = a;
                }
                return json!({"bool": ok});
            }
            other => panic!("unknown op {other:?}"),
        }
        Value::Null
    }
}

fn err_rec_server(err: Option<String>) -> Value {
    match err {
        None => json!({"ok": true}),
        Some(e) => {
            json!({"err": e, "no_such_call": false, "already_answered": false, "not_supported": false})
        }
    }
}

fn err_rec(err: Option<DriverError>) -> Value {
    match err {
        None => json!({"ok": true}),
        Some(e) => json!({
            "err": e.message(), "no_such_call": e.is(Sentinel::NoSuchCall),
            "already_answered": e.is(Sentinel::AlreadyAnswered), "not_supported": e.is(Sentinel::NotSupported),
        }),
    }
}

async fn run_scenario(sc: Value) -> Result<(), String> {
    let name = go_str(&sc, "name");
    let ops = sc["ops"].as_array().unwrap().clone();
    let want = sc["out"].as_array().unwrap().clone();
    let no_start = sc.get("no_start").and_then(Value::as_bool).unwrap_or(false);
    let mut run = Run {
        ord_b: sc
            .get("ord_branches")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        branch_n: HashMap::new(),
        pend: Vec::new(),
        opend: Vec::new(),
        dials: HashMap::new(),
        h: None,
        outsider: None,
        srv: "127.0.0.1:0".parse().unwrap(),
        rtp_port: 0,
        sent_tags: HashSet::new(),
        sent_branches: HashSet::new(),
        tag_n: HashMap::new(),
        nonce_n: HashMap::new(),
        nonces: Vec::new(),
        cl: None,
        frames: None,
        audio: None,
        seq: 0,
        oseq: 0,
        last: None,
    };
    if !no_start {
        let mut cfg = json!({"device_id": "dev-1"}).as_object().cloned().unwrap();
        if let Some(extra) = sc.get("cfg").and_then(Value::as_object) {
            cfg.extend(extra.clone());
        }
        let h = Harness::with(Opts {
            cfg,
            ring: Some(ms(go_i(&sc, "ring_ms") as u64)),
            orphan: Some(ms(go_i(&sc, "orphan_ms") as u64)),
            strict_invites: false,
        })
        .await;
        run.srv = SocketAddr::new("127.0.0.1".parse().unwrap(), h.s.sip_addr().port());
        run.rtp_port = h.s.rtp_port();
        run.outsider = Some(Station::on(h.s.sip_addr(), "127.0.0.2").await);
        run.h = Some(h);
    }
    for (i, o) in ops.iter().enumerate() {
        let got = run.exec(o).await;
        if got != want[i] {
            return Err(format!(
                "scenario {name:?}, op {i} ({}):\n  got  {}\n  want {}",
                serde_json::to_string(o).unwrap(),
                serde_json::to_string(&got).unwrap(),
                serde_json::to_string(&want[i]).unwrap()
            ));
        }
    }
    if let Some(h) = &run.h {
        let _ = h.d.stop().await;
    }
    let _ = UdpSocket::bind("127.0.0.1:0").await; // keep the import honest on no_start runs
    Ok(())
}

/// Replays one golden file; `batch` scenarios run at once (the server is on real time).
async fn replay(file: &str, count: usize, batch: usize) {
    let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests/golden")
        .join(file);
    let all: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let scenarios = all.as_array().unwrap().clone();
    assert_eq!(
        scenarios.len(),
        count,
        "the number of scenarios in {file} changed"
    );
    let mut failures = Vec::new();
    // in batches: the server runs on real time and a hundred stations at once only add noise
    for batch in scenarios.chunks(batch) {
        let handles: Vec<_> = batch
            .iter()
            .cloned()
            .map(|sc| tokio::spawn(run_scenario(sc)))
            .collect();
        for h in handles {
            if let Err(e) = h.await.unwrap() {
                failures.push(e);
            }
        }
    }
    let ops: usize = scenarios
        .iter()
        .map(|s| s["ops"].as_array().unwrap().len())
        .sum();
    let observed: usize = scenarios
        .iter()
        .flat_map(|s| s["out"].as_array().unwrap().iter())
        .filter(|v| !v.is_null())
        .count();
    eprintln!(
        "{file}: replayed {} scenarios, {ops} operations, {observed} observations",
        scenarios.len()
    );
    assert!(
        failures.is_empty(),
        "{} scenario(s) differ from the Go server:\n{}",
        failures.len(),
        failures.join("\n")
    );
}

#[tokio::test]
async fn golden_server_scenarios() {
    // Go's hard-clamp gain and unity uplink, not the production soft limiter and AGC
    transport_rs::driver::gatesipserver::audio::set_legacy_audio(true);
    replay("gss_server.json", 82, 10).await;
}

/// The outbound call (dial.go): 52 scenarios of the real Go server dialling a scripted station.
#[tokio::test]
async fn golden_dial_scenarios() {
    // Go's hard-clamp gain and unity uplink, not the production soft limiter and AGC
    transport_rs::driver::gatesipserver::audio::set_legacy_audio(true);
    replay("gss_dial.json", 52, 8).await;
}
