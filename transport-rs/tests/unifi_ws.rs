//! The websocket client (`ws.go`), the console API client (`api.go`) and the events-frame
//! decoder (`ring.go`'s `eventMsg`) of the `unifi_protect` driver, below the driver: the frame
//! decoder byte by byte and under random input, the pump over an in-memory stream, the upgrade
//! against crafted servers, the HTTP client against crafted answers. These are the behaviours
//! the port adds around Go's tests (Go reaches `readMessage` and `dialWS` only through the
//! driver); the replay against Go's own answers is `golden_unifi.rs`. Loopback only.

mod common;

use std::sync::Arc;
use std::time::Duration;

use common::unifi::*;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::unifiprotect::api::{path_escape, ProtectApi, TalkbackSession};
use transport_rs::driver::unifiprotect::ring::{decode_event, EventMsg};
use transport_rs::driver::unifiprotect::tls::Tls;
use transport_rs::driver::unifiprotect::ws::{
    self, accept_for, encode_frame, pump, reason_for, scrub, Decoder, DialError, DialParams, Event,
    PumpTiming, WsConn, WsError, MAX_MESSAGE,
};
use transport_rs::gostd::net::{join_host_port, split_host_port};
use transport_rs::util::base64_encode;

fn ms(n: u64) -> Duration {
    Duration::from_millis(n)
}

fn drain(d: &mut Decoder) -> (Vec<Event>, Option<WsError>) {
    let mut out = vec![];
    loop {
        match d.next_event() {
            Ok(Some(e)) => out.push(e),
            Ok(None) => return (out, None),
            Err(e) => return (out, Some(e)),
        }
    }
}

fn whole(bytes: &[u8]) -> (Vec<Event>, Option<WsError>) {
    let mut d = Decoder::new();
    d.push(bytes);
    drain(&mut d)
}

fn bytewise(bytes: &[u8]) -> (Vec<Event>, Option<WsError>) {
    let mut d = Decoder::new();
    let mut out = vec![];
    for b in bytes {
        d.push(&[*b]);
        let (mut e, err) = drain(&mut d);
        out.append(&mut e);
        if err.is_some() {
            return (out, err);
        }
    }
    (out, None)
}

// ---- the frame decoder -----------------------------------------------------------------------

#[test]
fn the_decoder_gives_the_same_events_fed_whole_or_one_byte_at_a_time() {
    let mut masked = vec![0x81, 0x80 | 5, 1, 2, 3, 4];
    masked.extend(
        b"hello"
            .iter()
            .enumerate()
            .map(|(i, b)| b ^ [1u8, 2, 3, 4][i % 4]),
    );
    let big = vec![b'z'; 70_000];
    let cases: Vec<Vec<u8>> = vec![
        frame(0x1, b"text"),
        [frame(0x9, b"p"), frame(0x1, b"after")].concat(),
        [
            raw_frame(0x01, b"ab"),
            frame(0x9, b"mid"),
            raw_frame(0x80, b"cd"),
        ]
        .concat(), // fragments with a ping inside
        [
            raw_frame(0x02, b"bin"),
            raw_frame(0x80, b"ary"),
            frame(0x1, b"t"),
        ]
        .concat(), // a binary message is dropped
        masked,
        frame(0x1, &big),
        frame(0x1, &[b'y'; 126]),
        frame(0x1, &[b'y'; 125]),
        frame(0x1, b""),
        [
            frame(0x8, b""),
            frame(0x1, b"never read after close but decoded here"),
        ]
        .concat(),
        raw_frame(0xC1, b"x"),                                   // reserved bit
        raw_frame(0x83, b"x"),                                   // unknown opcode
        raw_frame(0x80, b"x"), // a continuation with nothing to continue
        [raw_frame(0x01, b"a"), raw_frame(0x01, b"b")].concat(), // a new message before the last one finished
        [frame(0x1, b"ok"), vec![0x81]].concat(),                // a truncated header at the end
    ];
    for (i, c) in cases.iter().enumerate() {
        assert_eq!(whole(c), bytewise(c), "case {i}");
    }
    let (ev, err) = whole(&cases[3]);
    assert_eq!(ev, vec![Event::Message(b"t".to_vec())]);
    assert!(err.is_none());
}

#[test]
fn the_decoder_errors_are_gos() {
    let err = |b: Vec<u8>| whole(&b).1;
    assert_eq!(err(raw_frame(0xC1, b"x")), Some(WsError::Protocol));
    assert_eq!(err(raw_frame(0xA1, b"x")), Some(WsError::Protocol)); // rsv2
    assert_eq!(err(raw_frame(0x91 | 0x80, b"x")), Some(WsError::Protocol));
    assert_eq!(err(raw_frame(0x83, b"x")), Some(WsError::Protocol));
    assert_eq!(err(raw_frame(0x8F, b"")), Some(WsError::Protocol));
    assert_eq!(err(raw_frame(0x80, b"x")), Some(WsError::Protocol));
    assert_eq!(
        err([raw_frame(0x01, b"a"), raw_frame(0x02, b"b")].concat()),
        Some(WsError::Protocol)
    );
    assert_eq!(
        WsError::Closed.to_string(),
        "websocket closed by the console"
    );
    assert_eq!(WsError::Protocol.to_string(), "websocket protocol error");
    assert_eq!(WsError::TooBig.to_string(), "websocket message too large");
    assert_eq!(WsError::Eof.to_string(), "EOF");
    assert_eq!(WsError::UnexpectedEof.to_string(), "unexpected EOF");
    assert_eq!(WsError::Timeout.to_string(), "i/o timeout");
}

#[test]
fn an_oversize_frame_is_refused_from_its_header_before_a_payload_byte_is_held() {
    // 127 + 8-byte length of MAX_MESSAGE + 1: refused with nothing after the header
    let mut hdr = vec![0x81, 127];
    hdr.extend_from_slice(&((MAX_MESSAGE as u64) + 1).to_be_bytes());
    let mut d = Decoder::new();
    d.push(&hdr);
    assert_eq!(d.next_event(), Err(WsError::TooBig));
    // exactly MAX_MESSAGE is legal: the decoder waits for the payload and holds only what came
    let mut hdr = vec![0x81, 127];
    hdr.extend_from_slice(&(MAX_MESSAGE as u64).to_be_bytes());
    let mut d = Decoder::new();
    d.push(&hdr);
    d.push(&vec![b'a'; 1000]);
    assert_eq!(d.next_event(), Ok(None));
    assert!(d.buffered() < 2000);
    // a huge announced length (2^63) is refused the same way, control frames included
    for op in [0x81u8, 0x89, 0x88, 0x8A] {
        let mut hdr = vec![op, 127];
        hdr.extend_from_slice(&(1u64 << 63).to_be_bytes());
        assert_eq!(whole(&hdr).1, Some(WsError::TooBig), "{op:#x}");
    }
    // fragments that add up to more than the cap
    let part = vec![b'x'; 600_000];
    let mut b = raw_frame(0x01, &part);
    b.extend(raw_frame(0x00, &part));
    assert_eq!(whole(&b).1, Some(WsError::TooBig));
    // the same two parts that fit are one message
    let part = vec![b'x'; 500_000];
    let mut b = raw_frame(0x01, &part);
    b.extend(raw_frame(0x80, &part));
    let (ev, err) = whole(&b);
    assert!(err.is_none());
    assert!(matches!(&ev[..], [Event::Message(m)] if m.len() == 1_000_000));
}

#[test]
fn control_frames_follow_rfc_6455_limits() {
    // DEV-796 D9: Go answers a 300-byte or non-final ping; Rust refuses both (RFC 6455 5.5)
    assert_eq!(
        whole(&raw_frame(0x09, &vec![b'p'; 300])).1,
        Some(WsError::Protocol)
    );
    assert_eq!(whole(&raw_frame(0x09, b"p")).1, Some(WsError::Protocol)); // not final
    assert_eq!(whole(&raw_frame(0x08, b"")).1, Some(WsError::Protocol));
    assert_eq!(whole(&raw_frame(0x0A, b"")).1, Some(WsError::Protocol));
    // a 1 MiB header alone is enough to refuse it (nothing after the header is held)
    let mut hdr = vec![0x89, 127];
    hdr.extend_from_slice(&(1u64 << 20).to_be_bytes());
    assert_eq!(whole(&hdr).1, Some(WsError::Protocol));
    // exactly 125 bytes, final, is answered
    let (ev, err) = whole(&frame(0x9, &[b'p'; 125]));
    assert!(err.is_none());
    assert!(matches!(&ev[..], [Event::Ping(p)] if p.len() == 125));
    let (ev, _) = whole(&[frame(0xA, b"pong"), frame(0x8, b"bye")].concat());
    assert_eq!(ev, vec![Event::Pong, Event::Close]);
}

#[test]
fn a_run_of_empty_continuation_frames_is_cut() {
    let mut stream = raw_frame(0x01, b"a"); // a text message begun, never finished
    for _ in 0..1000 {
        stream.extend(raw_frame(0x00, b""));
    }
    assert_eq!(whole(&stream).1, Some(WsError::Protocol));
    // a few empty fragments inside a real message are fine, and a payload resets the run
    let mut ok = raw_frame(0x01, b"a");
    for _ in 0..3 {
        ok.extend(raw_frame(0x00, b""));
    }
    ok.extend(raw_frame(0x80, b"b"));
    let (ev, err) = whole(&ok);
    assert!(err.is_none());
    assert_eq!(ev, vec![Event::Message(b"ab".to_vec())]);
}

/// xorshift64*, so the fuzz is the same on every run.
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545F4914F6CDD1D)
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
}

#[test]
fn the_decoder_survives_random_input_and_never_holds_more_than_a_frame() {
    let mut rng = Rng(0x9E3779B97F4A7C15);
    let mut events = 0usize;
    let mut errors = 0usize;
    for round in 0..4000 {
        let mut d = Decoder::new();
        // a stream of plausible frame heads followed by random bytes, in random chunks
        let mut stream = Vec::new();
        for _ in 0..rng.below(6) {
            let b0 = [
                0x81, 0x89, 0x8A, 0x88, 0x01, 0x80, 0x02, 0x82, 0xC1, 0x83, 0x00,
            ][rng.below(11)];
            let len_code = [0u8, 5, 125, 126, 127, 0x80 | 3, 0x80 | 126][rng.below(7)];
            stream.push(b0);
            stream.push(len_code);
            for _ in 0..rng.below(40) {
                stream.push(rng.next() as u8);
            }
        }
        for _ in 0..rng.below(300) {
            stream.push(rng.next() as u8);
        }
        let mut at = 0;
        'feed: while at < stream.len() {
            let n = 1 + rng
                .below(64)
                .min(stream.len() - at - 1 + 1 - 1)
                .min(stream.len() - at - 1);
            let n = n.max(1).min(stream.len() - at);
            d.push(&stream[at..at + n]);
            at += n;
            loop {
                match d.next_event() {
                    Ok(Some(_)) => events += 1,
                    Ok(None) => break,
                    Err(_) => {
                        errors += 1;
                        break 'feed;
                    }
                }
            }
            assert!(
                d.buffered() <= MAX_MESSAGE + 14 + 64,
                "round {round}: {} bytes held",
                d.buffered()
            );
        }
    }
    assert!(
        events > 100 && errors > 100,
        "the fuzz exercised nothing: {events} events, {errors} errors"
    );
}

#[test]
fn encoded_frames_are_masked_final_and_the_right_width() {
    for n in [0usize, 1, 125, 126, 127, 65_535, 65_536, 70_000] {
        let payload: Vec<u8> = (0..n).map(|i| (i * 7) as u8).collect();
        let f = encode_frame(0x9, &payload, [0xDE, 0xAD, 0xBE, 0xEF]);
        assert_eq!(f[0], 0x89, "FIN and the opcode");
        assert!(f[1] & 0x80 != 0, "masked");
        let width = match n {
            n if n < 126 => 0,
            n if n < 65_536 => 2,
            _ => 8,
        };
        assert_eq!(f.len(), 2 + width + 4 + n, "n={n}");
        let (op, p, used) = parse_masked(&f).unwrap();
        assert_eq!((op, used), (9, f.len()));
        assert_eq!(p, payload);
    }
    let f = encode_frame(0x8, &[], [1, 2, 3, 4]);
    assert_eq!(f, vec![0x88, 0x80, 1, 2, 3, 4]);
}

#[test]
fn host_port_helpers_follow_net() {
    for (hp, want) in [
        ("h:443", Some(("h", "443"))),
        ("10.0.0.5:7443", Some(("10.0.0.5", "7443"))),
        ("[::1]:443", Some(("::1", "443"))),
        ("h:", Some(("h", ""))),
        (":80", Some(("", "80"))),
        ("h", None),
        ("::1", None),
        ("[::1]", None),
        ("[::1", None),
        ("a:b:c", None),
        ("[::1]x:80", None),
        ("h[x:80", None),
        ("h]:80", None),
        ("", None),
    ] {
        assert_eq!(
            split_host_port(hp)
                .ok()
                .map(|(h, p)| (h.to_string(), p.to_string())),
            want.map(|(h, p)| (h.to_string(), p.to_string())),
            "{hp:?}"
        );
    }
    assert_eq!(join_host_port("h", "443"), "h:443");
    assert_eq!(join_host_port("::1", "443"), "[::1]:443");
}

#[test]
fn base64_accept_and_escape_vectors() {
    // RFC 6455 section 1.3
    assert_eq!(
        accept_for("dGhlIHNhbXBsZSBub25jZQ=="),
        "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    );
    for (i, o) in [
        ("", ""),
        ("f", "Zg=="),
        ("fo", "Zm8="),
        ("foo", "Zm9v"),
        ("foob", "Zm9vYg=="),
        ("fooba", "Zm9vYmE="),
        ("foobar", "Zm9vYmFy"),
    ] {
        assert_eq!(base64_encode(i.as_bytes()), o);
    }
    assert_eq!(path_escape("cam1"), "cam1");
    assert_eq!(
        path_escape("a/b;c,d?e f%g#h\"i<j>k é"),
        "a%2Fb%3Bc%2Cd%3Fe%20f%25g%23h%22i%3Cj%3Ek%20%C3%A9"
    );
    assert_eq!(
        path_escape("a-b_c.d~e$f&g+h=i:j@k"),
        "a-b_c.d~e$f&g+h=i:j@k"
    );
    assert_eq!(scrub("a SECRET b SECRET", "SECRET"), "a *** b ***");
    assert_eq!(scrub("a b", ""), "a b");
    assert_eq!(
        reason_for(&DialError::Status(503)),
        "events stream refused (HTTP 503)"
    );
    assert_eq!(
        reason_for(&DialError::Cert),
        "console certificate not trusted (verify_ssl)"
    );
    assert_eq!(
        reason_for(&DialError::Msg("tls: boom".into())),
        "console TLS handshake failed"
    );
    assert_eq!(
        reason_for(&DialError::Msg("connect: refused".into())),
        "console unreachable"
    );
    assert_eq!(
        reason_for(&DialError::Msg("upgrade: EOF".into())),
        "console unreachable"
    );
    assert_eq!(reason_for(&DialError::Cancelled), "console unreachable");
}

// ---- the events frame -------------------------------------------------------------------------

fn ev(typ: &str, id: &str, mk: &str, it: &str, dev: &str, end: Option<&str>) -> EventMsg {
    EventMsg {
        typ: typ.into(),
        id: id.into(),
        model_key: mk.into(),
        item_type: it.into(),
        device: dev.into(),
        end: end.map(|s| s.to_string()),
    }
}

#[test]
fn the_event_frame_decodes_as_json_unmarshal_does() {
    let ok = |s: &str| decode_event(s.as_bytes());
    let full = r#"{"type":"add","item":{"id":"e","modelKey":"event","type":"ring","device":"c","start":1}}"#;
    assert_eq!(ok(full), Some(ev("add", "e", "event", "ring", "c", None)));
    // whitespace, key case, unknown fields, nested noise
    assert_eq!(
        ok(" \n{\"TYPE\":\"update\",\"Item\":{\"ID\":\"e\",\"MODELKEY\":\"event\",\"Type\":\"ring\",\"DEVICE\":\"c\",\"End\": 5 , \"extra\":{\"a\":[1,2]}},\"zz\":null}\t"),
        Some(ev("update", "e", "event", "ring", "c", Some("5")))
    );
    // null at the top is a zero message; null fields are no-ops; end keeps its raw text
    assert_eq!(ok("null"), Some(EventMsg::default()));
    assert_eq!(
        ok(r#"{"type":null,"item":null}"#),
        Some(EventMsg::default())
    );
    assert_eq!(
        ok(r#"{"item":{"id":null,"end":null}}"#),
        Some(EventMsg {
            end: Some("null".into()),
            ..Default::default()
        })
    );
    assert!(!ok(r#"{"item":{"end":null}}"#).unwrap().has_end());
    assert!(ok(r#"{"item":{"end":0}}"#).unwrap().has_end());
    assert!(ok(r#"{"item":{"end":false}}"#).unwrap().has_end());
    assert!(ok(r#"{"item":{"end":"x"}}"#).unwrap().has_end());
    assert!(ok(r#"{"item":{"end":{"a":1}}}"#).unwrap().has_end());
    assert!(!ok(r#"{"item":{}}"#).unwrap().has_end());
    // a repeated key: the last one wins; a repeated `item` is merged into the first
    assert_eq!(
        ok(
            r#"{"type":"a","type":"b","item":{"id":"1","device":"d"},"item":{"id":"2","type":"ring"}}"#
        ),
        Some(ev("b", "2", "", "ring", "d", None))
    );
    assert_eq!(
        ok(r#"{"item":{"end":5,"end":null}}"#)
            .unwrap()
            .end
            .as_deref(),
        Some("null")
    );
    // errors: any wrong type in a field we read, syntax, non-objects
    for bad in [
        r#"{"type":5}"#,
        r#"{"type":true}"#,
        r#"{"item":"ring"}"#,
        r#"{"item":[]}"#,
        r#"{"item":5}"#,
        r#"{"item":{"id":5}}"#,
        r#"{"item":{"modelKey":[]}}"#,
        r#"{"item":{"type":{}}}"#,
        r#"{"item":{"device":false}}"#,
        r#"{"type":"add","item":{"id":"e"}"#,
        r#"{"type":"add"} trailing"#,
        r#"[]"#,
        r#""s""#,
        "5",
        "true",
        "",
        "   ",
        "{",
        r#"{"type":"a\q"}"#,
    ] {
        assert_eq!(ok(bad), None, "{bad:?}");
    }
    // a type error anywhere makes the whole frame an error, even with the ring fields right
    assert_eq!(
        ok(
            r#"{"type":"add","item":{"id":"e","modelKey":"event","type":"ring","device":"c"},"type":7}"#
        ),
        None
    );
    // text that is not UTF-8 (Go would replace the bytes; the port ignores the frame)
    assert_eq!(
        decode_event(&[b'{', b'"', 0xff, b'"', b':', b'1', b'}']),
        None
    );
    // escapes decode
    assert_eq!(
        ok(r#"{"item":{"id":"aA\n\"q\""}}"#).unwrap().id,
        "aA\n\"q\""
    );
}

// ---- the pump over an in-memory stream ------------------------------------------------------------

fn timing(ping: u64, wait: u64) -> PumpTiming {
    PumpTiming {
        ping_interval: ms(ping),
        pong_wait: ms(wait),
    }
}

fn pipe() -> (WsConn, tokio::io::DuplexStream) {
    let (a, b) = tokio::io::duplex(1 << 20);
    (WsConn::from_io(Box::new(a), &[]), b)
}

/// Reads client frames from `s` until `pred` holds for the frames so far, within 3 s.
async fn client_frames_until(
    s: &mut tokio::io::DuplexStream,
    pred: impl Fn(&[(u8, Vec<u8>)]) -> bool,
) -> Vec<(u8, Vec<u8>)> {
    let mut buf = Vec::new();
    let mut frames = Vec::new();
    let mut tmp = [0u8; 4096];
    let end = tokio::time::Instant::now() + Duration::from_secs(3);
    while !pred(&frames) {
        let n = tokio::time::timeout_at(end, s.read(&mut tmp))
            .await
            .expect("timed out")
            .unwrap();
        assert!(
            n > 0,
            "the client closed the stream; frames so far {frames:?}"
        );
        buf.extend_from_slice(&tmp[..n]);
        while let Some((op, p, used)) = parse_masked(&buf) {
            buf.drain(..used);
            frames.push((op, p));
        }
    }
    frames
}

#[tokio::test]
async fn the_pump_answers_pings_delivers_messages_and_pings_on_its_interval() {
    let (conn, mut server) = pipe();
    let got = Arc::new(std::sync::Mutex::new(Vec::<Vec<u8>>::new()));
    let (g, cancel) = (got.clone(), CancelToken::new());
    let c2 = cancel.clone();
    let h = tokio::spawn(async move {
        pump(conn, &c2, timing(60, 5000), move |m| {
            g.lock().unwrap().push(m.to_vec())
        })
        .await
    });
    server.write_all(&frame(0x9, b"hello")).await.unwrap();
    server.write_all(&frame(0x1, b"one")).await.unwrap();
    let frames = client_frames_until(&mut server, |f| {
        f.contains(&(0xA, b"hello".to_vec())) && f.iter().filter(|x| x.0 == 0x9).count() >= 2
    })
    .await;
    assert!(
        frames.iter().all(|f| f.0 == 0x9 || f.0 == 0xA),
        "{frames:?}"
    );
    assert_eq!(*got.lock().unwrap(), vec![b"one".to_vec()]);
    cancel.cancel();
    assert_eq!(h.await.unwrap(), WsError::Cancelled);
    // a close frame followed (the pump closes on cancel)
    let mut rest = Vec::new();
    let _ = server.read_to_end(&mut rest).await;
    assert!(
        parse_masked(&rest).is_some_and(|(op, ..)| op == 8),
        "{rest:?}"
    );
}

#[tokio::test]
async fn the_pump_answers_a_close_with_a_close_and_ends() {
    let (conn, mut server) = pipe();
    let h =
        tokio::spawn(
            async move { pump(conn, &CancelToken::new(), timing(5000, 5000), |_| {}).await },
        );
    server.write_all(&frame(0x8, b"")).await.unwrap();
    assert_eq!(h.await.unwrap(), WsError::Closed);
    let mut rest = Vec::new();
    let _ = server.read_to_end(&mut rest).await;
    // exactly one close frame came back
    let (op, _, used) = parse_masked(&rest).expect("a close reply");
    assert_eq!((op, used), (8, rest.len()), "{rest:?}");
}

#[tokio::test]
async fn the_pump_tells_a_clean_eof_from_a_cut_frame() {
    for (bytes, want) in [
        (frame(0x1, b"whole"), WsError::Eof),
        (vec![], WsError::Eof),
        (vec![0x81], WsError::UnexpectedEof),
        (
            [frame(0x1, b"ok"), vec![0x81, 5, b'a']].concat(),
            WsError::UnexpectedEof,
        ),
        ([raw_frame(0x01, b"a")].concat(), WsError::Eof), // a fragment, then the stream ends at a frame boundary
    ] {
        let (conn, mut server) = pipe();
        server.write_all(&bytes).await.unwrap();
        drop(server);
        assert_eq!(
            pump(conn, &CancelToken::new(), timing(5000, 5000), |_| {}).await,
            want,
            "{bytes:?}"
        );
    }
}

#[tokio::test]
async fn the_pump_gives_up_on_a_silent_console_and_every_frame_restarts_the_wait() {
    let (conn, mut server) = pipe();
    let t0 = tokio::time::Instant::now();
    let h =
        tokio::spawn(
            async move { pump(conn, &CancelToken::new(), timing(5000, 300), |_| {}).await },
        );
    // a frame every 150 ms keeps it alive for well over the 300 ms wait, including a fragment
    // (not a message) and a binary frame
    for i in 0..5 {
        tokio::time::sleep(ms(150)).await;
        let f = match i {
            0 => frame(0xA, b""),
            1 => raw_frame(0x02, b"b"), // a binary fragment: not a message, still a frame
            2 => raw_frame(0x80, b"c"),
            3 => frame(0x1, b"{}"),
            _ => frame(0xA, b""),
        };
        server.write_all(&f).await.unwrap();
    }
    assert!(!h.is_finished());
    assert_eq!(h.await.unwrap(), WsError::Timeout);
    let el = t0.elapsed();
    assert!(el >= ms(750 + 250) && el < ms(2000), "{el:?}");
    // half a frame does not restart the wait
    let (conn, mut server) = pipe();
    let h =
        tokio::spawn(
            async move { pump(conn, &CancelToken::new(), timing(5000, 300), |_| {}).await },
        );
    server.write_all(&[0x81, 100]).await.unwrap();
    tokio::time::sleep(ms(150)).await;
    server.write_all(b"abc").await.unwrap();
    let t1 = tokio::time::Instant::now();
    assert_eq!(h.await.unwrap(), WsError::Timeout);
    assert!(t1.elapsed() < ms(250), "{:?}", t1.elapsed());
}

#[tokio::test]
async fn the_pump_survives_a_stalled_peer_by_timing_out_writes_not_by_hanging() {
    // a peer that never reads: a small duplex fills up, the pong write blocks, and the write
    // timeout (5 s, Go's) ends the pump with an I/O error. The read deadline cannot fire first:
    // `pump` awaits a pong write inline, outside its `select!` (R2-152)
    let (a, mut b) = tokio::io::duplex(64);
    let conn = WsConn::from_io(Box::new(a), &[]);
    let h =
        tokio::spawn(
            async move { pump(conn, &CancelToken::new(), timing(5000, 400), |_| {}).await },
        );
    for _ in 0..50 {
        let _ = tokio::time::timeout(ms(20), b.write_all(&frame(0x9, &[7u8; 100]))).await;
    }
    let t0 = tokio::time::Instant::now();
    let r = tokio::time::timeout(Duration::from_secs(8), h)
        .await
        .expect("the pump hung")
        .unwrap();
    assert!(matches!(r, WsError::Io(_)), "{r:?}");
    assert!(
        t0.elapsed() < Duration::from_secs(6),
        "the write timeout is 5 s: {:?}",
        t0.elapsed()
    );
}

// ---- the upgrade against crafted servers ------------------------------------------------------------

/// Serves one connection: reads the request head, then runs `f` with the stream and the head.
async fn crafted<F, Fut>(f: F) -> std::net::SocketAddr
where
    F: FnOnce(tokio::net::TcpStream, String) -> Fut + Send + 'static,
    Fut: std::future::Future<Output = ()> + Send + 'static,
{
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move {
        let (mut s, _) = l.accept().await.unwrap();
        let mut buf = Vec::new();
        let mut tmp = [0u8; 1024];
        while !buf.windows(4).any(|w| w == b"\r\n\r\n") {
            let n = s.read(&mut tmp).await.unwrap_or(0);
            if n == 0 {
                return;
            }
            buf.extend_from_slice(&tmp[..n]);
        }
        f(s, String::from_utf8_lossy(&buf).to_string()).await;
    });
    addr
}

fn accept_line(head: &str) -> String {
    accept_for(header(head, "Sec-WebSocket-Key").unwrap())
}

async fn dial_plain(addr: std::net::SocketAddr, t: Duration) -> Result<WsConn, DialError> {
    let host = addr.to_string();
    let tls = Tls::new(false);
    ws::dial(
        &CancelToken::new(),
        &DialParams {
            scheme: "http",
            host: &host,
            path: "/p",
            api_key: TEST_KEY,
            tls: &tls,
            dial_timeout: t,
        },
    )
    .await
}

#[tokio::test]
async fn the_upgrade_accepts_a_101_and_keeps_frames_that_came_with_it() {
    let addr = crafted(|mut s, head| async move {
        let mut resp = format!(
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nsec-websocket-accept:   {}  \r\n\r\n",
            accept_line(&head)
        )
        .into_bytes();
        resp.extend(frame(0x1, b"first"));
        resp.extend(frame(0x1, b"second"));
        s.write_all(&resp).await.unwrap();
        tokio::time::sleep(ms(300)).await;
    })
    .await;
    let conn = dial_plain(addr, Duration::from_secs(2))
        .await
        .expect("dial");
    let got = Arc::new(std::sync::Mutex::new(Vec::<Vec<u8>>::new()));
    let (g, cancel) = (got.clone(), CancelToken::new());
    let c2 = cancel.clone();
    let h = tokio::spawn(async move {
        pump(conn, &cancel, timing(5000, 5000), move |m| {
            g.lock().unwrap().push(m.to_vec());
            if g.lock().unwrap().len() == 2 {
                c2.cancel();
            }
        })
        .await
    });
    assert_eq!(h.await.unwrap(), WsError::Cancelled);
    assert_eq!(
        *got.lock().unwrap(),
        vec![b"first".to_vec(), b"second".to_vec()]
    );
}

#[tokio::test]
async fn the_upgrade_refusals_carry_the_status_and_nothing_else() {
    for (resp, want) in [
        (
            "HTTP/1.1 401 Unauthorized\r\nContent-Length: 19\r\n\r\nTOKEN-IN-ERROR-BODY",
            DialError::Status(401),
        ),
        (
            "HTTP/1.1 503 Service Unavailable\r\n\r\n",
            DialError::Status(503),
        ),
        (
            "HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/x\r\n\r\n",
            DialError::Status(302),
        ), // never followed
        ("HTTP/1.0 200 OK\r\n\r\n", DialError::Status(200)),
        (
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n",
            DialError::Msg("upgrade: bad Sec-WebSocket-Accept".into()),
        ),
        (
            "HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: AAAA\r\n\r\n",
            DialError::Msg("upgrade: bad Sec-WebSocket-Accept".into()),
        ),
        (
            "garbage\r\n\r\n",
            DialError::Msg("upgrade: malformed HTTP response".into()),
        ),
        (
            "HTTP/1.1 10 X\r\n\r\n",
            DialError::Msg("upgrade: malformed HTTP response".into()),
        ),
        (
            "HTTP/1.1 abc X\r\n\r\n",
            DialError::Msg("upgrade: malformed HTTP response".into()),
        ),
        (
            "FTP/1.1 101 X\r\n\r\n",
            DialError::Msg("upgrade: malformed HTTP response".into()),
        ),
    ] {
        let r = resp.to_string();
        let addr = crafted(move |mut s, _| async move {
            s.write_all(r.as_bytes()).await.unwrap();
            tokio::time::sleep(ms(200)).await;
        })
        .await;
        let e = dial_plain(addr, Duration::from_secs(2))
            .await
            .err()
            .expect(resp);
        assert_eq!(e, want, "{resp:?}");
        assert!(!e.to_string().contains("TOKEN-IN-ERROR-BODY"));
    }
}

#[tokio::test]
async fn the_upgrade_survives_a_head_in_many_pieces_and_refuses_one_that_never_ends() {
    let addr = crafted(|mut s, head| async move {
        let resp = format!(
            "HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: {}\r\n\r\n",
            accept_line(&head)
        );
        for chunk in resp.as_bytes().chunks(7) {
            s.write_all(chunk).await.unwrap();
            tokio::time::sleep(ms(5)).await;
        }
        tokio::time::sleep(ms(200)).await;
    })
    .await;
    assert!(dial_plain(addr, Duration::from_secs(3)).await.is_ok());

    // a head that never ends is cut at 64 KiB
    let addr = crafted(|mut s, _| async move {
        let line = format!("X-Pad: {}\r\n", "a".repeat(1000));
        let _ = s.write_all(b"HTTP/1.1 101 Switching Protocols\r\n").await;
        for _ in 0..200 {
            if s.write_all(line.as_bytes()).await.is_err() {
                return;
            }
        }
        tokio::time::sleep(ms(300)).await;
    })
    .await;
    let e = dial_plain(addr, Duration::from_secs(3))
        .await
        .err()
        .unwrap();
    assert_eq!(
        e,
        DialError::Msg("upgrade: response header too large".into())
    );

    // EOF before a head
    let addr = crafted(|s, _| async move { drop(s) }).await;
    let e = dial_plain(addr, Duration::from_secs(3))
        .await
        .err()
        .unwrap();
    assert_eq!(e, DialError::Msg("upgrade: EOF".into()));
}

#[tokio::test]
async fn the_upgrade_times_out_and_can_be_cancelled_and_connect_errors_name_no_key() {
    let addr = crafted(|s, _| async move {
        let _s = s;
        tokio::time::sleep(Duration::from_secs(3)).await
    })
    .await;
    let t0 = tokio::time::Instant::now();
    let e = dial_plain(addr, ms(300)).await.err().unwrap();
    assert_eq!(e, DialError::Msg("upgrade: i/o timeout".into()));
    assert!(t0.elapsed() >= ms(280) && t0.elapsed() < ms(1500));

    let addr = crafted(|s, _| async move {
        let _s = s;
        tokio::time::sleep(Duration::from_secs(3)).await
    })
    .await;
    let cancel = CancelToken::new();
    let c2 = cancel.clone();
    tokio::spawn(async move {
        tokio::time::sleep(ms(100)).await;
        c2.cancel();
    });
    let host = addr.to_string();
    let tls = Tls::new(false);
    let r = ws::dial(
        &cancel,
        &DialParams {
            scheme: "http",
            host: &host,
            path: "/p",
            api_key: TEST_KEY,
            tls: &tls,
            dial_timeout: Duration::from_secs(5),
        },
    )
    .await;
    assert_eq!(r.err(), Some(DialError::Cancelled));

    // nothing listens
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let dead = l.local_addr().unwrap();
    drop(l);
    let e = dial_plain(dead, Duration::from_secs(2))
        .await
        .err()
        .unwrap();
    assert!(
        matches!(&e, DialError::Msg(m) if m.starts_with("connect: ") && !m.contains(TEST_KEY)),
        "{e:?}"
    );
    assert_eq!(reason_for(&e), "console unreachable");
}

#[tokio::test]
async fn a_tls_dial_to_a_plain_port_and_a_plain_dial_to_a_tls_port_fail_as_tls() {
    // https to a server that speaks plain HTTP: the handshake fails, "tls:" -> TLS handshake failed
    let addr = crafted(|mut s, _| async move {
        let _ = s.write_all(b"HTTP/1.1 400 Bad Request\r\n\r\n").await;
        tokio::time::sleep(ms(200)).await;
    })
    .await;
    let host = addr.to_string();
    let tls = Tls::new(false);
    let e = ws::dial(
        &CancelToken::new(),
        &DialParams {
            scheme: "https",
            host: &host,
            path: "/p",
            api_key: TEST_KEY,
            tls: &tls,
            dial_timeout: Duration::from_secs(2),
        },
    )
    .await
    .err()
    .unwrap();
    assert!(
        matches!(&e, DialError::Msg(m) if m.starts_with("tls:")),
        "{e:?}"
    );
    assert_eq!(reason_for(&e), "console TLS handshake failed");

    // a console that accepts TCP and then says nothing: the handshake times out as "tls:"
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let silent = l.local_addr().unwrap();
    tokio::spawn(async move {
        let (_s, _) = l.accept().await.unwrap();
        tokio::time::sleep(Duration::from_secs(3)).await;
    });
    let host = silent.to_string();
    let e = ws::dial(
        &CancelToken::new(),
        &DialParams {
            scheme: "https",
            host: &host,
            path: "/p",
            api_key: TEST_KEY,
            tls: &tls,
            dial_timeout: ms(300),
        },
    )
    .await
    .err()
    .unwrap();
    assert_eq!(e, DialError::Msg("tls: i/o timeout".into()));
}

// ---- the console API ------------------------------------------------------------------------------------

fn api(f: &FakeProtect) -> ProtectApi {
    ProtectApi::new("http", &f.host(), TEST_KEY, true)
}

async fn mint(f: &FakeProtect) -> Result<String, String> {
    api(f).rtsps_url(&CancelToken::new(), "cam1", "high").await
}

#[tokio::test]
async fn the_api_decodes_the_rtsps_answer_as_json_unmarshal_does() {
    let f = FakeProtect::new().await;
    let reply = |body: &str| {
        format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
    };
    for (body, want) in [
        (r#"{"high":"rtsps://h/t"}"#, Ok("rtsps://h/t")),
        (
            r#"{"high":"rtsps://h/a","high":"rtsps://h/b"}"#,
            Ok("rtsps://h/b"),
        ), // the last wins
        (
            r#"{"low":"rtsps://h/l","high":"rtsps://h/t"}"#,
            Ok("rtsps://h/t"),
        ),
        (
            r#"{"high":null,"x":"y"}"#,
            Err(r#"rtsps-stream: no "high" stream in the response"#),
        ),
        (
            "null",
            Err(r#"rtsps-stream: no "high" stream in the response"#),
        ),
        (
            "{}",
            Err(r#"rtsps-stream: no "high" stream in the response"#),
        ),
        (
            r#"{"high":"http://x"}"#,
            Err("rtsps-stream: unexpected stream scheme"),
        ),
        (
            r#"{"high":"RTSPS://x"}"#,
            Err("rtsps-stream: unexpected stream scheme"),
        ),
        (r#"{"high":5}"#, Err("rtsps-stream: undecodable response")),
        (
            r#"{"high":["a"]}"#,
            Err("rtsps-stream: undecodable response"),
        ),
        (
            r#"{"low":5,"high":"rtsps://h/t"}"#,
            Err("rtsps-stream: undecodable response"),
        ), // any wrong value fails the decode
        ("[]", Err("rtsps-stream: undecodable response")),
        (r#""s""#, Err("rtsps-stream: undecodable response")),
        ("not json", Err("rtsps-stream: undecodable response")),
        ("", Err("rtsps-stream: undecodable response")),
        (
            r#"{"high":"rtsps://h/t"} x"#,
            Err("rtsps-stream: undecodable response"),
        ),
    ] {
        f.set_raw_reply(reply(body).as_bytes());
        let got = mint(&f).await;
        assert_eq!(
            got.as_ref().map(|s| s.as_str()).map_err(|e| e.as_str()),
            want,
            "{body:?}"
        );
    }
}

#[tokio::test]
async fn the_api_reads_chunked_answers_and_skips_interim_ones_and_caps_the_body() {
    let f = FakeProtect::new().await;
    let body = r#"{"high":"rtsps://h/t"}"#;
    let (a, b) = body.split_at(7);
    let chunked = format!("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n{:x};ext=1\r\n{a}\r\n{:X}\r\n{b}\r\n0\r\nTrailer: x\r\n\r\n", a.len(), b.len());
    f.set_raw_reply(chunked.as_bytes());
    assert_eq!(mint(&f).await.unwrap(), "rtsps://h/t");
    // 100 Continue, then the real answer
    let r = format!(
        "HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n{body}",
        body.len()
    );
    f.set_raw_reply(r.as_bytes());
    assert_eq!(mint(&f).await.unwrap(), "rtsps://h/t");
    // close-delimited
    f.set_raw_reply(format!("HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n{body}").as_bytes());
    assert_eq!(mint(&f).await.unwrap(), "rtsps://h/t");
    // a body over 64 KiB is cut at 64 KiB (and then fails to decode); the rest is not read
    let long = format!("{{\"high\":\"rtsps://{}\"}}", "a".repeat(70_000));
    f.set_raw_reply(
        format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n{long}",
            long.len()
        )
        .as_bytes(),
    );
    assert_eq!(
        mint(&f).await.unwrap_err(),
        "rtsps-stream: undecodable response"
    );
    f.set_raw_reply(
        format!(
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n{:x}\r\n{long}\r\n0\r\n\r\n",
            long.len()
        )
        .as_bytes(),
    );
    assert_eq!(
        mint(&f).await.unwrap_err(),
        "rtsps-stream: undecodable response"
    );
    // a content-length that promises more than arrives: the read error, not the status
    f.set_raw_reply(b"HTTP/1.1 401 X\r\nContent-Length: 50\r\n\r\nshort");
    let e = mint(&f).await.unwrap_err();
    assert_eq!(e, "POST /proxy/protect/integration/v1/cameras/cam1/rtsps-stream: reading response: unexpected EOF");
    // a bad chunk size
    f.set_raw_reply(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nZZ\r\nabc\r\n0\r\n\r\n");
    assert!(mint(&f)
        .await
        .unwrap_err()
        .contains("reading response: invalid byte in chunk length"));
}

#[tokio::test]
async fn the_api_never_follows_a_redirect_and_names_the_status_only() {
    let f = FakeProtect::new().await;
    for code in [301u16, 302, 307, 308, 400, 403, 404, 429, 500, 502] {
        f.set_raw_reply(format!("HTTP/1.1 {code} X\r\nLocation: http://127.0.0.1:1/\r\nContent-Length: 19\r\n\r\nTOKEN-IN-ERROR-BODY").as_bytes());
        let e = mint(&f).await.unwrap_err();
        assert_eq!(
            e,
            format!("POST /proxy/protect/integration/v1/cameras/cam1/rtsps-stream: HTTP {code}")
        );
    }
    // 204 and 304 have no body to wait for
    for code in [204u16, 304] {
        f.set_raw_reply(format!("HTTP/1.1 {code} X\r\n\r\n").as_bytes());
        let e = mint(&f).await.unwrap_err();
        assert!(
            e.ends_with(&format!("HTTP {code}")) || e.contains("undecodable") || code == 204,
            "{e}"
        );
    }
}

#[tokio::test]
async fn the_api_times_out_can_be_cancelled_and_never_prints_the_key() {
    let f = FakeProtect::new().await;
    f.set_silent(true);
    let mut a = api(&f);
    a.set_timeout(ms(300));
    let t0 = tokio::time::Instant::now();
    let e = a
        .rtsps_url(&CancelToken::new(), "cam1", "high")
        .await
        .unwrap_err();
    assert!(
        t0.elapsed() >= ms(280) && t0.elapsed() < ms(2000),
        "{:?}",
        t0.elapsed()
    );
    assert!(e.starts_with("POST /proxy/protect/integration/v1/cameras/cam1/rtsps-stream: Post \"http://127.0.0.1:"), "{e}");
    assert!(
        e.ends_with("context deadline exceeded (Client.Timeout exceeded while awaiting headers)"),
        "{e}"
    );
    assert!(!e.contains(TEST_KEY));

    let cancel = CancelToken::new();
    let c2 = cancel.clone();
    tokio::spawn(async move {
        tokio::time::sleep(ms(100)).await;
        c2.cancel();
    });
    let e = api(&f)
        .rtsps_url(&cancel, "cam1", "high")
        .await
        .unwrap_err();
    assert!(e.ends_with("context canceled"), "{e}");

    // refused connection: the form is Go's, and a key in the host (a bad config) is scrubbed
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let dead = l.local_addr().unwrap();
    drop(l);
    let a = ProtectApi::new("http", &dead.to_string(), TEST_KEY, true);
    let e = a
        .rtsps_url(&CancelToken::new(), "cam1", "high")
        .await
        .unwrap_err();
    assert!(
        e.contains("dial tcp 127.0.0.1:") && e.contains("connection refused"),
        "{e}"
    );
    let a = ProtectApi::new("http", &format!("{TEST_KEY}.invalid:1"), TEST_KEY, true);
    let e = a
        .rtsps_url(&CancelToken::new(), "cam1", "high")
        .await
        .unwrap_err();
    assert!(!e.contains(TEST_KEY) && e.contains("***"), "{e}");
}

#[tokio::test]
async fn the_api_puts_the_camera_id_in_the_path_escaped() {
    let f = FakeProtect::new().await;
    let a = api(&f);
    let _ = a.rtsps_url(&CancelToken::new(), "a/b c?d", "high").await;
    let _ = a.talkback_session(&CancelToken::new(), "x;y").await;
    assert_eq!(
        f.reqs(),
        vec![
            "POST /proxy/protect/integration/v1/cameras/a%2Fb%20c%3Fd/rtsps-stream",
            "POST /proxy/protect/integration/v1/cameras/x%3By/talkback-session",
        ]
    );
    // a POST without a body says Content-Length: 0 and sends no Content-Type
    let h = &f.heads()[1];
    assert_eq!(header(h, "Content-Length"), Some("0"));
    assert_eq!(header(h, "Content-Type"), None);
    assert_eq!(header(h, "X-API-KEY"), Some(TEST_KEY));
    assert_eq!(header(h, "Connection"), Some("close"));
    // a quality that needs JSON escaping
    let _ = a.rtsps_url(&CancelToken::new(), "c", "hi\"<gh>").await;
    assert_eq!(
        f.bodies()[2],
        "{\"qualities\":[\"hi\\\"\\u003cgh\\u003e\"]}"
    );
}

#[tokio::test]
async fn the_talkback_session_decodes_as_json_unmarshal_does() {
    let f = FakeProtect::new().await;
    let a = api(&f);
    let get = |body: &str| {
        f.set_talkback(body);
        let a = &a;
        async move { a.talkback_session(&CancelToken::new(), "cam1").await }
    };
    assert_eq!(
        get(r#"{"url":"rtp://192.168.2.9:7004","codec":"opus","samplingRate":24000,"bitsPerSample":16}"#).await.unwrap(),
        TalkbackSession { url: "rtp://192.168.2.9:7004".into(), codec: "opus".into(), sampling_rate: 24000, bits_per_sample: 16 }
    );
    // key case, extra keys, nulls, a missing field
    assert_eq!(
        get(r#"{"URL":"u","Codec":null,"SAMPLINGRATE":8000,"x":[1]}"#)
            .await
            .unwrap(),
        TalkbackSession {
            url: "u".into(),
            codec: "".into(),
            sampling_rate: 8000,
            bits_per_sample: 0
        }
    );
    assert_eq!(get("null").await.unwrap(), TalkbackSession::default());
    assert_eq!(get("{}").await.unwrap(), TalkbackSession::default());
    for bad in [
        r#"{"samplingRate":24000.5}"#,
        r#"{"samplingRate":"24000"}"#,
        r#"{"samplingRate":2.4e4}"#,
        r#"{"url":5}"#,
        r#"{"codec":[]}"#,
        "[]",
        "x",
        "",
    ] {
        assert_eq!(
            get(bad).await.unwrap_err(),
            "talkback-session: undecodable response",
            "{bad:?}"
        );
    }
    assert_eq!(
        get(r#"{"samplingRate":-5}"#).await.unwrap().sampling_rate,
        -5
    );
}

#[tokio::test]
async fn the_api_over_tls_follows_verify_ssl() {
    let (acceptor, cert) = self_signed_acceptor();
    let f = FakeProtect::with_tls(Some(acceptor)).await;
    // verify off: any certificate
    let a = ProtectApi::new("https", &f.host(), TEST_KEY, false);
    assert_eq!(
        a.rtsps_url(&CancelToken::new(), "cam1", "high")
            .await
            .unwrap(),
        "rtsps://nvr.example:7441/TOKEN123?enableSrtp"
    );
    // verify on, system pool: refused, in Go's words
    let a = ProtectApi::new("https", &f.host(), TEST_KEY, true);
    let e = a
        .rtsps_url(&CancelToken::new(), "cam1", "high")
        .await
        .unwrap_err();
    assert!(
        e.contains(
            "tls: failed to verify certificate: x509: certificate signed by unknown authority"
        ),
        "{e}"
    );
    assert!(!e.contains(TEST_KEY));
    // verify on, the certificate trusted
    let mut roots = rustls::RootCertStore::empty();
    roots.add(cert).unwrap();
    let a = ProtectApi::with_tls(
        "https",
        &f.host(),
        TEST_KEY,
        Arc::new(Tls::with_roots(roots)),
    );
    assert_eq!(
        a.rtsps_url(&CancelToken::new(), "cam1", "high")
            .await
            .unwrap(),
        "rtsps://nvr.example:7441/TOKEN123?enableSrtp"
    );
    // https against a plain-HTTP console that answers at once: a TLS error
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let plain = l.local_addr().unwrap();
    tokio::spawn(async move {
        let (mut s, _) = l.accept().await.unwrap();
        let _ = s
            .write_all(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            .await;
        tokio::time::sleep(ms(500)).await;
    });
    let a = ProtectApi::new("https", &plain.to_string(), TEST_KEY, false);
    let e = a
        .rtsps_url(&CancelToken::new(), "cam1", "high")
        .await
        .unwrap_err();
    assert!(e.contains(": tls: "), "{e}");
}

#[test]
fn the_ca_bundle_is_read_where_go_reads_it() {
    use transport_rs::driver::unifiprotect::tls::system_roots_from;
    let ck = rcgen::generate_simple_self_signed(vec!["x.example".to_string()]).unwrap();
    let pem = |der: &[u8]| {
        let b = base64_encode(der);
        let mut s = String::from("-----BEGIN CERTIFICATE-----\n");
        for c in b.as_bytes().chunks(64) {
            s.push_str(std::str::from_utf8(c).unwrap());
            s.push('\n');
        }
        s.push_str("-----END CERTIFICATE-----\n");
        s
    };
    let dir = std::env::temp_dir().join(format!("unifi-ca-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join("d")).unwrap();
    let file = dir.join("bundle.pem");
    std::fs::write(
        &file,
        format!(
            "junk\n{}\n-----BEGIN CERTIFICATE-----\nnot base64 !!\n-----END CERTIFICATE-----\n",
            pem(ck.cert.der())
        ),
    )
    .unwrap();
    std::fs::write(dir.join("d").join("one.pem"), pem(ck.cert.der())).unwrap();
    std::fs::write(dir.join("d").join("note.txt"), "no certificate here").unwrap();
    // SSL_CERT_FILE replaces the file list; SSL_CERT_DIR replaces the directory list
    assert_eq!(
        system_roots_from(Some(file.to_str().unwrap()), Some("/nonexistent")).len(),
        1
    );
    assert_eq!(
        system_roots_from(
            Some("/nonexistent/file"),
            Some(dir.join("d").to_str().unwrap())
        )
        .len(),
        1
    );
    // an unreadable file list falls through; an empty environment value counts as unset
    assert_eq!(
        system_roots_from(Some("/nonexistent/file"), Some("/nonexistent")).len(),
        0
    );
    assert_eq!(
        system_roots_from(
            Some(file.to_str().unwrap()),
            Some(&format!(
                "{}:{}",
                dir.join("d").display(),
                dir.join("zz").display()
            ))
        )
        .len(),
        1,
        "the same certificate from a file and a directory is one root"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn a_host_or_key_that_could_end_a_header_is_never_sent() {
    // the request builders refuse it before any connection (configure refuses it too)
    let f = FakeProtect::new().await;
    let a = ProtectApi::new("http", &f.host(), "k\r\nX-Evil: 1", true);
    let e = a
        .rtsps_url(&CancelToken::new(), "cam1", "high")
        .await
        .unwrap_err();
    assert!(
        e.contains("refusing a host or key with control characters") && !e.contains("X-Evil"),
        "{e}"
    );
    let a = ProtectApi::new(
        "http",
        &format!("{}\r\nX-Evil: 1", f.host()),
        TEST_KEY,
        true,
    );
    assert!(a
        .rtsps_url(&CancelToken::new(), "cam1", "high")
        .await
        .is_err());
    assert!(f.reqs().is_empty(), "{:?}", f.reqs());
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let host = l.local_addr().unwrap().to_string();
    let tls = Tls::new(false);
    for (h, k) in [
        (host.clone(), "k\r\nX: y".to_string()),
        (format!("{host}\r\nX: y"), TEST_KEY.to_string()),
    ] {
        let e = ws::dial(
            &CancelToken::new(),
            &DialParams {
                scheme: "http",
                host: &h,
                path: "/p",
                api_key: &k,
                tls: &tls,
                dial_timeout: ms(500),
            },
        )
        .await
        .err()
        .unwrap();
        assert!(
            matches!(&e, DialError::Msg(m) if m.contains("refusing"))
                || matches!(&e, DialError::Msg(m) if m.starts_with("connect:")),
            "{e:?}"
        );
        assert!(!e.to_string().contains("X: y"));
    }
    // nothing ever connected for the refused ones
    assert!(tokio::time::timeout(ms(200), l.accept()).await.is_err());
}

#[tokio::test]
async fn the_api_names_the_certificate_failure_and_rejects_odd_framing() {
    let (acceptor, cert) = self_signed_acceptor();
    let f = FakeProtect::with_tls(Some(acceptor)).await;
    let mut roots = rustls::RootCertStore::empty();
    roots.add(cert).unwrap();
    // trusted, but under a name the certificate does not cover
    let a = ProtectApi::with_tls(
        "https",
        &format!("localhost:{}", f.addr.port()),
        TEST_KEY,
        Arc::new(Tls::with_roots(roots)),
    );
    let e = a
        .rtsps_url(&CancelToken::new(), "cam1", "high")
        .await
        .unwrap_err();
    assert!(e.contains("tls: failed to verify certificate: x509: certificate is not valid for the requested name"), "{e}");
    // framing
    let p = FakeProtect::new().await;
    let api = ProtectApi::new("http", &p.host(), TEST_KEY, true);
    p.set_raw_reply(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n+3\r\nabc\r\n0\r\n\r\n");
    let e = api
        .rtsps_url(&CancelToken::new(), "c", "high")
        .await
        .unwrap_err();
    assert!(e.contains("invalid byte in chunk length"), "{e}");
    p.set_raw_reply(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nContent-Length: 5\r\n\r\nabcde");
    let e = api
        .rtsps_url(&CancelToken::new(), "c", "high")
        .await
        .unwrap_err();
    assert!(e.contains("malformed HTTP response"), "{e}");
    // R2-76: a length that is not a plain non-negative integer is refused (net/http says
    // `bad Content-Length`), and so is a chunk that is not followed by CRLF
    for cl in ["abc", "-1", "+2", "2x"] {
        p.set_raw_reply(format!("HTTP/1.1 200 OK\r\nContent-Length: {cl}\r\n\r\n{{}}").as_bytes());
        let e = api
            .rtsps_url(&CancelToken::new(), "c", "high")
            .await
            .unwrap_err();
        assert!(e.contains("malformed HTTP response"), "{cl}: {e}");
    }
    p.set_raw_reply(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabcXY0\r\n\r\n");
    let e = api
        .rtsps_url(&CancelToken::new(), "c", "high")
        .await
        .unwrap_err();
    assert!(e.contains("malformed chunked encoding"), "{e}");
    // a bad length next to a chunked body is not looked at
    let body = r#"{"high":"rtsps://h/t"}"#;
    p.set_raw_reply(
        format!(
            "HTTP/1.1 200 OK\r\nContent-Length: abc\r\nTransfer-Encoding: chunked\r\n\r\n{:x}\r\n{body}\r\n0\r\n\r\n",
            body.len()
        )
        .as_bytes(),
    );
    assert_eq!(
        api.rtsps_url(&CancelToken::new(), "c", "high")
            .await
            .unwrap(),
        "rtsps://h/t"
    );
    // equal duplicates are fine
    p.set_raw_reply(
        format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {0}\r\nContent-Length: {0}\r\n\r\n{body}",
            body.len()
        )
        .as_bytes(),
    );
    assert_eq!(
        api.rtsps_url(&CancelToken::new(), "c", "high")
            .await
            .unwrap(),
        "rtsps://h/t"
    );
}

/// R1-12: with `verify_ssl: false` a certificate the library cannot parse (here a version 1
/// one) still fails the handshake, because the handshake signature is checked against
/// its key. That is not a trust failure: the installer must not be told to turn `verify_ssl`
/// off when it is off already.
#[tokio::test]
async fn an_unreadable_certificate_with_verify_off_is_not_reported_as_untrusted() {
    let f = FakeProtect::with_tls(Some(unreadable_cert_acceptor())).await;
    // the websocket path
    let tls = Tls::new(false);
    let host = f.host();
    let e = ws::dial(
        &CancelToken::new(),
        &DialParams {
            scheme: "https",
            host: &host,
            path: "/p",
            api_key: TEST_KEY,
            tls: &tls,
            dial_timeout: Duration::from_secs(3),
        },
    )
    .await
    .err()
    .expect("a handshake against a certificate webpki cannot read must fail");
    assert_ne!(
        e,
        DialError::Cert,
        "told to turn verify_ssl off while it is off"
    );
    assert!(
        matches!(&e, DialError::Msg(m) if m.starts_with("tls:")),
        "{e:?}"
    );
    assert_eq!(ws::reason_for(&e), "console TLS handshake failed");
    // the REST path
    let a = ProtectApi::new("https", &host, TEST_KEY, false);
    let e = a
        .rtsps_url(&CancelToken::new(), "cam1", "high")
        .await
        .unwrap_err();
    assert!(
        !e.contains("failed to verify certificate") && e.contains(": tls: "),
        "{e}"
    );
    assert!(!e.contains(TEST_KEY));
}

/// R1-42: the CA bundle and the two directories are read once per process, not once per
/// `ProtectApi` (a door answer builds one per call).
#[tokio::test]
async fn the_system_pool_is_read_once_per_process() {
    let (acceptor, _cert) = self_signed_acceptor();
    let f = FakeProtect::with_tls(Some(acceptor)).await;
    let ask = || async {
        let a = ProtectApi::new("https", &f.host(), TEST_KEY, true);
        let e = a
            .rtsps_url(&CancelToken::new(), "cam1", "high")
            .await
            .unwrap_err();
        assert!(e.contains("unknown authority"), "{e}");
    };
    ask().await;
    let after_first = transport_rs::driver::unifiprotect::tls::system_builds();
    assert!(after_first >= 1);
    ask().await;
    ask().await;
    assert_eq!(
        transport_rs::driver::unifiprotect::tls::system_builds(),
        after_first,
        "the system pool was read again for a new client"
    );
}

/// R1-93: `%q` of the stream quality is `strconv.Quote` (`\a \b \f \v`, not `\x07`).
#[tokio::test]
async fn a_missing_stream_names_the_quality_as_go_quotes_it() {
    let p = FakeProtect::new().await;
    p.set_rtsps("{}");
    let api = ProtectApi::new("http", &p.host(), TEST_KEY, true);
    let e = api
        .rtsps_url(&CancelToken::new(), "c", "\u{7}\u{8}\u{c}\u{b}\u{200b}")
        .await
        .unwrap_err();
    assert_eq!(
        e, "rtsps-stream: no \"\\a\\b\\f\\v\\u200b\" stream in the response",
        "{e}"
    );
}

/// R1-96: both decoders repair a body that is not UTF-8 as Go does, and trim JSON white space
/// only (four characters, not Unicode's).
#[tokio::test]
async fn the_stream_map_decodes_a_non_utf8_body_and_trims_json_white_space_only() {
    let p = FakeProtect::new().await;
    let api = ProtectApi::new("http", &p.host(), TEST_KEY, true);
    let reply = |body: &[u8]| {
        let mut r =
            format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n", body.len()).into_bytes();
        r.extend_from_slice(body);
        r
    };
    p.set_raw_reply(&reply(b" \r\n\t{\"high\":\"rtsps://h/t\xff\"}\n "));
    assert_eq!(
        api.rtsps_url(&CancelToken::new(), "c", "high")
            .await
            .unwrap(),
        "rtsps://h/t\u{fffd}"
    );
    p.set_raw_reply(&reply("\u{a0}{\"high\":\"rtsps://h/t\"}".as_bytes()));
    let e = api
        .rtsps_url(&CancelToken::new(), "c", "high")
        .await
        .unwrap_err();
    assert_eq!(e, "rtsps-stream: undecodable response");
}

/// R2-7: the REST client dials what `net/http` dials: `[::1]` and `host:` dial port 80 (the
/// error text of the refused dial names the address), an unparsable port is the bare parse error,
/// and the `Host` header is the host as `NewRequest` leaves it.
#[tokio::test]
async fn the_rest_client_dials_net_https_target_for_odd_host_forms() {
    let f = FakeProtect::new().await;
    let api = ProtectApi::new("http", &f.host(), TEST_KEY, true);
    api.rtsps_url(&CancelToken::new(), "c", "high")
        .await
        .unwrap();
    assert_eq!(header(&f.heads()[0], "Host"), Some(f.host().as_str()));
    // an unparsable port is the bare parse error `http.NewRequest` returns, not a dial
    let api = ProtectApi::new("http", "127.0.0.1:abc", TEST_KEY, true);
    let e = api
        .rtsps_url(&CancelToken::new(), "c", "high")
        .await
        .unwrap_err();
    assert!(
        e.starts_with("parse \"http://127.0.0.1:abc/proxy/protect/")
            && e.ends_with("invalid port \":abc\" after host"),
        "{e}"
    );
    // `[::1]` with no port dials `[::1]:80`, not `[[::1]]:80`; `host:` dials port 80
    for (host, want) in [
        ("[::1]", "dial tcp [::1]:80: connect: "),
        ("127.0.0.1:", "dial tcp 127.0.0.1:80: connect: "),
    ] {
        let api = ProtectApi::new("http", host, TEST_KEY, true);
        let e = api
            .rtsps_url(&CancelToken::new(), "c", "high")
            .await
            .unwrap_err();
        assert!(e.contains(want), "{host}: {e}");
    }
}
