//! Port of `rehearsal_test.go` (E3): the offline rehearsal of the server against gate-sip's
//! answers. Each dialogue in `tests/rehearsal/*.dlg` (copied unchanged from
//! `transport/pkg/driver/gatesipserver/testdata/rehearsal`) is a script of what a Dahua VTO does
//! on the wire plus what the server side is asked to do. The same script is replayed against the
//! Rust server and its normalised transcript is diffed with the one RECORDED FROM THE REAL
//! gate-sip (`golden/<id>.gatesip.txt`). A MATCH dialogue must have an empty diff; a DIFFERS
//! dialogue must reproduce the diff the Go test pins (`golden/<id>.diff`) byte for byte, so the
//! Rust server differs from gate-sip in exactly the ways the Go server does and in no others.
//!
//! The gate-sip side is not re-recorded here: `rehearsal_record_gate_sip` (`TestRehearsalRecordGateSip`)
//! is `#[ignore]`d and has Go's skip condition (`REHEARSAL_GATESIP_BIN` unset), but its recording
//! body (the gate-sip process and its /health target) is not ported; see PARITY.md.

mod common;

use std::collections::{HashMap, HashSet};
use std::net::SocketAddr;
use std::path::PathBuf;
use std::time::Duration;

use common::*;
use serde_json::json;
use tokio::net::UdpSocket;
use transport_rs::driver::gatesipserver::ServerError;
use transport_rs::driver::Driver;

const PASSWORD: &str = "s3cretpass";

struct Step {
    kind: String,
    args: HashMap<String, String>,
    raw: String,
}

struct Dialogue {
    id: String,
    source: String,
    verdict: String,
    title: String,
    ring_ms: u64,
    orphan_ms: u64,
    steps: Vec<Step>,
}

fn dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/rehearsal")
}

fn load_dialogues() -> Vec<Dialogue> {
    let mut files: Vec<_> = std::fs::read_dir(dir())
        .unwrap()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "dlg"))
        .collect();
    files.sort();
    assert!(!files.is_empty(), "no dialogues in {}", dir().display());
    let mut out = Vec::new();
    for f in files {
        let text = std::fs::read_to_string(&f).unwrap();
        let mut d = Dialogue {
            id: f.file_stem().unwrap().to_string_lossy().into_owned(),
            source: String::new(),
            verdict: String::new(),
            title: String::new(),
            ring_ms: 45000,
            orphan_ms: 15000,
            steps: Vec::new(),
        };
        let lines: Vec<&str> = text.split('\n').map(|l| l.trim_end_matches('\r')).collect();
        let mut i = 0;
        while i < lines.len() {
            let ln = lines[i];
            if let Some(rest) = ln.strip_prefix("# ") {
                if let Some((k, v)) = rest.split_once(':') {
                    let v = v.trim().to_string();
                    match k.trim() {
                        "source" => d.source = v,
                        "title" => d.title = v,
                        "verdict" => d.verdict = v,
                        "ring-ms" => d.ring_ms = v.parse().unwrap_or(0),
                        "orphan-ms" => d.orphan_ms = v.parse().unwrap_or(0),
                        _ => {}
                    }
                }
            } else if let Some(rest) = ln.strip_prefix(">>> ") {
                let w: Vec<&str> = rest.split_whitespace().collect();
                let mut st = Step {
                    kind: w[0].to_string(),
                    args: HashMap::new(),
                    raw: String::new(),
                };
                for a in &w[1..] {
                    match a.split_once('=') {
                        Some((k, v)) => st.args.insert(k.to_string(), v.to_string()),
                        None => st.args.insert((*a).to_string(), String::new()),
                    };
                }
                if st.kind == "send" {
                    let mut msg = Vec::new();
                    i += 1;
                    while i < lines.len() && lines[i] != "." {
                        msg.push(lines[i]);
                        i += 1;
                    }
                    st.raw = msg.join("\n");
                }
                d.steps.push(st);
            }
            i += 1;
        }
        assert!(
            !d.source.is_empty() && !d.verdict.is_empty() && !d.title.is_empty(),
            "{}: needs # source, # title and # verdict",
            f.display()
        );
        out.push(d);
    }
    out
}

/// `reTagSent` over `s`: (start of the whole match, start of the tag value, end).
fn tag_matches(s: &str) -> Vec<(usize, usize, usize)> {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i + 5 <= b.len() {
        if &b[i..i + 5] == b";tag=" {
            let st = i + 5;
            let mut e = st;
            while e < b.len()
                && (b[e].is_ascii_alphanumeric() || matches!(b[e], b'.' | b'_' | b'-'))
            {
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

/// `z9hG4bK[A-Za-z0-9]+` -> `z9hG4bK<B>`.
fn mask_branches(s: &str) -> String {
    let mut out = String::new();
    let mut rest = s;
    while let Some(i) = rest.find("z9hG4bK") {
        let after = &rest[i + 7..];
        let n = after.bytes().take_while(u8::is_ascii_alphanumeric).count();
        if n == 0 {
            out.push_str(&rest[..i + 7]);
            rest = after;
            continue;
        }
        out.push_str(&rest[..i]);
        out.push_str("z9hG4bK<B>");
        rest = &after[n..];
    }
    out.push_str(rest);
    out
}

struct Replayer<'a> {
    h: &'a Harness,
    conn: UdpSocket,
    rtp_conn: UdpSocket,
    outsider: UdpSocket,
    srv: SocketAddr,
    nonce: String,
    sent_tags: HashSet<String>,
    tag_n: HashMap<String, String>,
    nonce_n: HashMap<String, String>,
    out: Vec<String>,
    seq: u16,
}

impl<'a> Replayer<'a> {
    async fn new(h: &'a Harness) -> Replayer<'a> {
        Replayer {
            h,
            conn: UdpSocket::bind("127.0.0.1:0").await.unwrap(),
            rtp_conn: UdpSocket::bind("127.0.0.1:0").await.unwrap(),
            outsider: UdpSocket::bind("127.0.0.2:0").await.unwrap(),
            srv: h.s.sip_addr(),
            nonce: String::new(),
            sent_tags: HashSet::new(),
            tag_n: HashMap::new(),
            nonce_n: HashMap::new(),
            out: Vec::new(),
            seq: 0,
        }
    }

    fn auth(&self, password: &str, nonce: &str, user: &str) -> String {
        digest_for(user, password, "VDP", nonce)
    }

    fn expand(&self, tpl: &str) -> String {
        let st = self.conn.local_addr().unwrap();
        tpl.replace("{{SRV}}", &self.srv.to_string())
            .replace("{{SRVIP}}", "127.0.0.1")
            .replace("{{ST}}", &st.to_string())
            .replace("{{STIP}}", "127.0.0.1")
            .replace(
                "{{RTP}}",
                &self.rtp_conn.local_addr().unwrap().port().to_string(),
            )
            .replace("{{AUTH_OK}}", &self.auth(PASSWORD, &self.nonce, "8001"))
            .replace(
                "{{AUTH_BAD}}",
                &self.auth("not-the-secret", &self.nonce, "8001"),
            )
            .replace(
                "{{AUTH_FORGED}}",
                &self.auth(PASSWORD, "0123456789abcdef0123456789abcdef", "8001"),
            )
            .replace("{{AUTH_NOFIELDS}}", "Digest username=\"8001\"")
    }

    async fn send(&mut self, tpl: &str) {
        let msg = self.expand(tpl);
        for (_, s, e) in tag_matches(&msg) {
            self.sent_tags.insert(msg[s..e].to_string());
        }
        let (head, body) = msg.split_once("\n\n").unwrap_or((&msg, ""));
        let mut hl: Vec<String> = head
            .split('\n')
            .filter(|l| !l.to_lowercase().starts_with("content-length:"))
            .map(str::to_string)
            .collect();
        let body = if body.is_empty() {
            String::new()
        } else {
            format!("{}\r\n", body.trim_end_matches('\n').replace('\n', "\r\n"))
        };
        hl.push(format!("Content-Length: {}", body.len()));
        let wire = format!("{}\r\n\r\n{body}", hl.join("\r\n"));
        self.conn.send_to(wire.as_bytes(), self.srv).await.unwrap();
        let first = head
            .split('\n')
            .next()
            .unwrap_or("")
            .replace(&self.srv.to_string(), "<SRV>");
        let kind = head
            .split('\n')
            .find_map(|l| l.strip_prefix("CSeq:").map(|v| v.trim().to_string()))
            .unwrap_or_else(|| "?".to_string());
        self.out.push(format!("SEND {first} (as {kind})"));
    }

    async fn recv(&mut self, window: Duration) {
        let mut buf = vec![0u8; 65535];
        let end = tokio::time::Instant::now() + window;
        let mut n = 0;
        while let Ok(Ok((k, _))) = tokio::time::timeout_at(end, self.conn.recv_from(&mut buf)).await
        {
            n += 1;
            let raw = String::from_utf8_lossy(&buf[..k]).into_owned();
            let lines = self.normalise(&raw);
            self.out.extend(lines);
        }
        if n == 0 {
            self.out.push("RECV (nothing)".to_string());
        }
    }

    /// Reduces one received message to the lines that carry meaning, masking what legitimately
    /// differs between runs and between the two servers' naming (see the Go original).
    fn normalise(&mut self, raw: &str) -> Vec<String> {
        let st = self.conn.local_addr().unwrap().to_string();
        let (head, body) = raw.split_once("\r\n\r\n").unwrap_or((raw, ""));
        let mut out = Vec::new();
        for (i, l) in head.split("\r\n").enumerate() {
            if l.to_lowercase().starts_with("user-agent:") {
                continue;
            }
            let mut l = l.to_string();
            if l.starts_with("Content-Length:") && l != "Content-Length: 0" {
                l = "Content-Length: <body>".to_string(); // the SDP s= line is each server's own text
            }
            l = l
                .replace(&st, "<ST>")
                .replace(&self.srv.to_string(), "<SRV>");
            l = mask_branches(&l);
            // server-minted tags become ordinals, tags we sent stay as they are
            let mut masked = String::new();
            let mut last = 0;
            for (m0, s, e) in tag_matches(&l) {
                masked.push_str(&l[last..m0]);
                let tag = l[s..e].to_string();
                if self.sent_tags.contains(&tag) {
                    masked.push_str(&l[m0..e]);
                } else {
                    let n = self.tag_n.len() + 1;
                    let ord = self
                        .tag_n
                        .entry(tag)
                        .or_insert_with(|| format!("<T{n}>"))
                        .clone();
                    masked.push_str(";tag=");
                    masked.push_str(&ord);
                }
                last = e;
            }
            masked.push_str(&l[last..]);
            l = masked;
            if let Some(a) = l.find("nonce=\"") {
                let rest = &l[a + 7..];
                if let Some(b) = rest.find('"') {
                    let nonce = rest[..b].to_string();
                    self.nonce = nonce.clone();
                    let n = self.nonce_n.len() + 1;
                    let ord = self
                        .nonce_n
                        .entry(nonce)
                        .or_insert_with(|| format!("<N{n}>"))
                        .clone();
                    l = format!("{}nonce=\"{}\"{}", &l[..a], ord, &rest[b + 1..]);
                }
            }
            if i == 0 {
                l = format!("RECV {}", l.strip_prefix("SIP/2.0 ").unwrap_or(&l));
            } else {
                l = format!("     {l}");
            }
            out.push(l);
        }
        for l in body.trim().split("\r\n") {
            if l.is_empty() || l.starts_with("o=") || l.starts_with("s=") {
                continue;
            }
            let mut l = l.replace("127.0.0.1", "<IP>");
            if let Some(rest) = l.strip_prefix("m=audio ") {
                let n = rest.bytes().take_while(u8::is_ascii_digit).count();
                if n > 0 {
                    l = format!("m=audio <RTP>{}", &rest[n..]);
                }
            }
            out.push(format!("     | {l}"));
        }
        out
    }

    async fn send_rtp(&mut self, outsider: bool, pt: u8, n: usize) {
        for _ in 0..n {
            self.seq = self.seq.wrapping_add(1);
            let pkt = rtp_packet(pt, self.seq, u32::from(self.seq) * 160, &[0u8; 160]);
            let dst = SocketAddr::new("127.0.0.1".parse().unwrap(), self.h.s.rtp_port());
            let from = if outsider {
                &self.outsider
            } else {
                &self.rtp_conn
            };
            from.send_to(&pkt, dst).await.unwrap();
            sleep_ms(5).await;
        }
    }

    fn state_line(&self) -> String {
        let sn = self.h.s.snapshot();
        format!(
            "registered={} ringing={} inCall={} dialogConfirmed={} calls={} answered={} rtpIn={}",
            sn.registered,
            sn.ringing,
            sn.in_call,
            sn.dialog_confirmed,
            sn.calls,
            sn.answered,
            sn.rtp_in
        )
    }

    async fn run(mut self, dl: &Dialogue) -> String {
        for s in &dl.steps {
            match s.kind.as_str() {
                "send" => self.send(&s.raw).await,
                "recv" => {
                    let w = s
                        .args
                        .get("window")
                        .map_or(ms(300), |v| parse_go_duration(v));
                    self.recv(w).await;
                }
                "sleep" => sleep(parse_go_duration(&s.args["d"])).await,
                "answer" => {
                    let r = match self.h.s.answer_current() {
                        Ok(()) => "answered".to_string(),
                        Err(ServerError::NoCall) => "no call ringing".to_string(), // gate-sip's words for the same refusal
                        Err(e) => e.to_string(),
                    };
                    self.out.push(format!("ACT answer -> {r}"));
                }
                "hangup" => {
                    let r = if self.h.s.hangup() {
                        "hungup"
                    } else {
                        "no call"
                    };
                    self.out.push(format!("ACT hangup -> {r}"));
                }
                "rtp" => {
                    let n: usize = s.args["n"].parse().unwrap();
                    let outsider = s.args.get("from").is_some_and(|f| f == "outsider");
                    let pt: u8 = s
                        .args
                        .get("pt")
                        .and_then(|p| p.parse().ok())
                        .filter(|&p| p != 0)
                        .unwrap_or(8);
                    self.send_rtp(outsider, pt, n).await;
                    sleep_ms(60).await;
                    let who = if outsider {
                        "outsider(127.0.0.2)"
                    } else {
                        "station"
                    };
                    self.out.push(format!("ACT rtp {n} packets from {who}"));
                }
                "state" => {
                    sleep_ms(40).await;
                    let l = self.state_line();
                    self.out.push(format!("STATE {l}"));
                }
                k => panic!("{}: unknown step {k:?}", dl.id),
            }
        }
        format!("{}\n", self.out.join("\n"))
    }
}

async fn sleep(d: Duration) {
    tokio::time::sleep(d).await;
}

/// `time.ParseDuration` for the forms the dialogues use (`500ms`, `2s`, `1m`).
fn parse_go_duration(s: &str) -> Duration {
    let (num, unit) = s.split_at(s.find(|c: char| c.is_alphabetic()).unwrap());
    let n: f64 = num.parse().unwrap();
    let secs = match unit {
        "ms" => n / 1000.0,
        "s" => n,
        "m" => n * 60.0,
        u => panic!("duration unit {u:?}"),
    };
    Duration::from_secs_f64(secs)
}

/// A small LCS diff: "- " lines only gate-sip has, "+ " lines only the server has. Empty when the
/// transcripts are identical. Same tie-breaking as the Go `lineDiff`.
fn line_diff(a: &str, b: &str) -> String {
    let x: Vec<&str> = a.trim_end_matches('\n').split('\n').collect();
    let y: Vec<&str> = b.trim_end_matches('\n').split('\n').collect();
    let mut lcs = vec![vec![0usize; y.len() + 1]; x.len() + 1];
    for i in (0..x.len()).rev() {
        for j in (0..y.len()).rev() {
            lcs[i][j] = if x[i] == y[j] {
                lcs[i + 1][j + 1] + 1
            } else {
                lcs[i + 1][j].max(lcs[i][j + 1])
            };
        }
    }
    let mut out = Vec::new();
    let (mut i, mut j) = (0, 0);
    while i < x.len() && j < y.len() {
        if x[i] == y[j] {
            i += 1;
            j += 1;
        } else if lcs[i + 1][j] >= lcs[i][j + 1] {
            out.push(format!("- {}", x[i]));
            i += 1;
        } else {
            out.push(format!("+ {}", y[j]));
            j += 1;
        }
    }
    while i < x.len() {
        out.push(format!("- {}", x[i]));
        i += 1;
    }
    while j < y.len() {
        out.push(format!("+ {}", y[j]));
        j += 1;
    }
    if out.is_empty() {
        String::new()
    } else {
        format!("{}\n", out.join("\n"))
    }
}

// TestRehearsal: every dialogue, against the transcripts recorded from the real gate-sip.
#[tokio::test]
async fn rehearsal() {
    let dialogues = load_dialogues();
    assert_eq!(dialogues.len(), 22, "the dialogues of the Go suite");
    let (mut matches, mut differs) = (0, 0);
    for dl in &dialogues {
        let want =
            std::fs::read_to_string(dir().join("golden").join(format!("{}.gatesip.txt", dl.id)))
                .unwrap_or_else(|e| panic!("no gate-sip golden for {}: {e}", dl.id));
        let h = Harness::with(Opts {
            cfg: json!({"password": PASSWORD}).as_object().cloned().unwrap(),
            ring: Some(ms(dl.ring_ms)),
            orphan: Some(ms(dl.orphan_ms)),
            strict_invites: false,
        })
        .await;
        let got = Replayer::new(&h).await.run(dl).await;
        h.d.stop().await.unwrap();
        let diff = line_diff(&want, &got);
        let pinned = std::fs::read_to_string(dir().join("golden").join(format!("{}.diff", dl.id)))
            .unwrap_or_default();
        match dl.verdict.as_str() {
            "MATCH" => {
                assert!(
                    diff.is_empty(),
                    "{} is declared MATCH but the Rust transcript differs from gate-sip's:\n{diff}",
                    dl.id
                );
                matches += 1;
            }
            "DIFFERS" => {
                assert!(
                    !diff.is_empty(),
                    "{} is declared DIFFERS but the transcripts are identical",
                    dl.id
                );
                assert_eq!(
                    diff, pinned,
                    "{}: the difference changed.\n--- pinned (golden/{}.diff)\n{pinned}--- now\n{diff}",
                    dl.id, dl.id
                );
                differs += 1;
            }
            v => panic!("{}: verdict must be MATCH or DIFFERS, got {v:?}", dl.id),
        }
        eprintln!(
            "REHEARSAL {:<4} {:<14} {:<8} {}",
            dl.id, dl.source, dl.verdict, dl.title
        );
    }
    eprintln!(
        "REHEARSAL TOTAL {} dialogues: {matches} MATCH, {differs} DIFFERS",
        dialogues.len()
    );
}

// TestRehearsalRecordGateSip: re-records gate-sip's side. It only runs when REHEARSAL_GATESIP_BIN
// names a gate-sip binary, and must only ever be run in a throwaway container with --network none
// (see the E3 report). Ported as far as the skip condition; the recording itself drives the
// gate-sip binary through its /health API (`newGateSipTarget`), which transport-rs does not have,
// so with the variable set this test fails loudly instead of recording nothing.
#[test]
#[ignore = "needs REHEARSAL_GATESIP_BIN (a gate-sip binary, run with --network none)"]
fn rehearsal_record_gate_sip() {
    let bin = std::env::var("REHEARSAL_GATESIP_BIN").unwrap_or_default();
    if bin.is_empty() {
        eprintln!("REHEARSAL_GATESIP_BIN not set: golden files are read, not re-recorded");
        return;
    }
    panic!(
        "REHEARSAL_GATESIP_BIN={bin}: recording the gate-sip side is done by the Go test \
         (TestRehearsalRecordGateSip); the gate-sip target is not ported"
    );
}
