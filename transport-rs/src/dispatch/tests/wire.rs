use super::*;

// TestCallDialer_ReplyOverTheRealSocketIsOkPlusCallID
#[tokio::test]
async fn the_dial_reply_over_the_real_socket_is_ok_plus_call_id() {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    let dir = std::env::temp_dir().join(format!("disp-{}-wirereply", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("t.sock");
    let srv = crate::ipc::Server::listen(&sock).unwrap();
    let hub = register(&srv, HubOptions::default());
    let a = Fake::new("door-a");
    *a.dialer.lock().unwrap() = Some(Ok(()));
    add(&hub, &a).await;
    let s = srv.clone();
    tokio::spawn(async move { s.serve().await });
    let c = tokio::net::UnixStream::connect(&sock).await.unwrap();
    let (rd, mut wr) = c.into_split();
    wr.write_all(b"{\"id\":3,\"method\":\"dial\",\"params\":{\"device_id\":\"door-a\"}}\n")
        .await
        .unwrap();
    let mut l = String::new();
    BufReader::new(rd).read_line(&mut l).await.unwrap();
    let v: Value = serde_json::from_str(&l).unwrap();
    let res = v["result"].as_object().expect("a result");
    assert!(
        res["ok"] == true
            && res["call_id"].as_str().is_some_and(|c| !c.is_empty())
            && res.len() == 2,
        "wire reply = {l}, want exactly {{ok:true, call_id:<string>}}"
    );
    hub.shutdown().await;
    srv.close().await;
    let _ = std::fs::remove_dir_all(&dir);
}

// R1-2, R2-13: the events a driver emits inside `dial_call` are on the wire before the `dial`
// reply (Go's `Notify` writes in the calling goroutine; PARITY 301). The notification writer
// takes 30 ms per note, so without the hub's reply barrier (or without its `flush`) the reply
// is written first: on a fast writer the notes won the race with or without the barrier, and
// the first version of this test could not fail (M5 survived).
#[tokio::test]
async fn the_dial_reply_follows_the_events_emitted_inside_the_handler() {
    let mut r = Rig::slow_notes("order", Duration::from_millis(30)).await;
    let f = Fake::new("front");
    *f.dialer.lock().unwrap() = Some(Ok(()));
    *f.emit_in_dial.lock().unwrap() = vec!["answered", "active"];
    add(&r.hub, &f).await;
    let (reply, notes) = r.call("dial", json!({"device_id": "front"})).await;
    assert!(reply.contains("\"result\""), "{reply}");
    let states: Vec<_> = notes
        .iter()
        .map(|n| n["params"]["state"].as_str().unwrap_or("?").to_string())
        .collect();
    assert_eq!(states, ["answered", "active"], "events before the reply");
    r.srv.close().await;
}

// New in 3b-2 (the Go tests of this path use fakes, or the observer): `dial` through the
// dispatcher to the REAL gate_sip_server, with a fake station on UDP loopback. The reply
// waits for the 200, carries the hub's `out-` id, the notifications are `call_state_changed`
// under that id and the device id (never `ringing`), and the station's BYE ends the call.
#[tokio::test]
async fn dial_reaches_the_real_driver_and_the_station_is_called() {
    use tokio::net::UdpSocket;
    let mut r = Rig::new("realdial").await;
    let port = {
        let s = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
        s.local_addr().unwrap().port()
    };
    let (ans, _) = r
        .call(
            "configure",
            server_cfg(json!({"device_id": "g1", "dial_enabled": true,
                "sip_listen": format!("127.0.0.1:{port}")})),
        )
        .await;
    assert!(ans.contains("InboundAudio"), "{ans}");
    let st = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let srv = format!("127.0.0.1:{port}");
    async fn recv(st: &UdpSocket) -> String {
        let mut b = vec![0u8; 4096];
        let (n, _) = tokio::time::timeout(Duration::from_secs(3), st.recv_from(&mut b))
            .await
            .expect("nothing from the server")
            .unwrap();
        String::from_utf8_lossy(&b[..n]).into_owned()
    }
    let hdr = |m: &str, name: &str| -> String {
        m.lines()
            .find_map(|l| {
                l.split_once(':')
                    .filter(|(k, _)| k.eq_ignore_ascii_case(name))
                    .map(|(_, v)| v.trim().to_string())
            })
            .unwrap_or_default()
    };
    let me = st.local_addr().unwrap();
    let register = |auth: &str| {
        let a = if auth.is_empty() {
            String::new()
        } else {
            format!("Authorization: {auth}\r\n")
        };
        format!(
            "REGISTER sip:127.0.0.1:{port} SIP/2.0\r\nVia: SIP/2.0/UDP {me};branch=z9hG4bKreg\r\n\
             From: <sip:8001@station>;tag=reg\r\nTo: <sip:8001@station>\r\nCall-ID: reg-1\r\n\
             CSeq: 1 REGISTER\r\nContact: <sip:8001@{me}>\r\nExpires: 300\r\n{a}Content-Length: 0\r\n\r\n"
        )
    };
    st.send_to(register("").as_bytes(), &srv).await.unwrap();
    let m401 = recv(&st).await;
    assert!(m401.starts_with("SIP/2.0 401"), "{m401}");
    let auth = crate::sip::authorization_for(
        &hdr(&m401, "WWW-Authenticate"),
        "8001",
        "12345678",
        "REGISTER",
        "sip:127.0.0.1",
    )
    .unwrap();
    st.send_to(register(&auth).as_bytes(), &srv).await.unwrap();
    assert!(recv(&st).await.starts_with("SIP/2.0 200"));

    // the dial request is answered only once the station has answered
    r.next += 1;
    let id = r.next;
    let req = format!("{{\"id\":{id},\"method\":\"dial\",\"params\":{{\"device_id\":\"g1\"}}}}\n");
    r.wr.write_all(req.as_bytes()).await.unwrap();
    let inv = recv(&st).await;
    assert!(
        inv.starts_with("INVITE sip:9901@127.0.0.1 SIP/2.0\r\n"),
        "{inv}"
    );
    let call_id = hdr(&inv, "Call-ID");
    assert!(
        call_id.starts_with("out-") && call_id.len() == 20,
        "{call_id}"
    );
    let sdp = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=x\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n\
         m=audio 40000 RTP/AVP 96\r\na=rtpmap:96 PCM/16000\r\n";
    let ok = format!(
        "SIP/2.0 200 OK\r\nVia: {}\r\nFrom: {}\r\nTo: {};tag=st1\r\nCall-ID: {call_id}\r\n\
         CSeq: {}\r\nContact: <sip:9901@{me}>\r\nContent-Type: application/sdp\r\nContent-Length: {}\r\n\r\n{sdp}",
        hdr(&inv, "Via"),
        hdr(&inv, "From"),
        hdr(&inv, "To"),
        hdr(&inv, "CSeq"),
        sdp.len()
    );
    st.send_to(ok.as_bytes(), &srv).await.unwrap();
    assert!(recv(&st).await.starts_with("ACK "));
    // everything the dispatcher says until the dial's answer
    let mut notes = Vec::new();
    let answer = loop {
        let v = r.line().await;
        if v["id"] == json!(id) {
            break v;
        }
        notes.push(v);
    };
    assert_eq!(
        answer["result"],
        json!({"ok": true, "call_id": call_id}),
        "{answer}"
    );
    // the station hangs up
    let bye = format!(
        "BYE sip:8001@127.0.0.1:{port} SIP/2.0\r\nVia: SIP/2.0/UDP {me};branch=z9hG4bKb\r\n\
         From: <sip:9901@127.0.0.1>;tag=st1\r\nTo: {}\r\nCall-ID: {call_id}\r\nCSeq: 3 BYE\r\nContent-Length: 0\r\n\r\n",
        hdr(&inv, "From")
    );
    st.send_to(bye.as_bytes(), &srv).await.unwrap();
    while !notes.iter().any(|n| n["params"]["state"] == "ended") {
        notes.push(r.line().await);
    }
    let states: Vec<_> = notes
        .iter()
        .filter(|n| n["method"] == "call_state_changed")
        .map(|n| {
            (
                n["params"]["state"].as_str().unwrap().to_string(),
                n["params"]["call_id"].clone(),
                n["params"]["device_id"].clone(),
            )
        })
        .collect();
    assert_eq!(
        states,
        vec![
            ("answered".to_string(), json!(call_id), json!("g1")),
            ("ended".to_string(), json!(call_id), json!("g1")),
        ],
        "{notes:?}"
    );
    assert!(
        notes.iter().all(|n| n["method"] != "ringing"),
        "an outbound call looked like a ring: {notes:?}"
    );
    assert!(
        notes.iter().any(|n| n["method"] == "capabilities_changed"
            && n["params"]["capabilities"]["CanDial"] == true),
        "the registration did not publish CanDial: {notes:?}"
    );
    let (ans, _) = r.call("deconfigure", json!({"device_id": "g1"})).await;
    assert!(ans.contains("\"ok\":true"), "{ans}");
    r.hub.shutdown().await;
    r.srv.close().await;
}
