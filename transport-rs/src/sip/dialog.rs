//! Port of `dialog.go`: the minimal audio-only SDP, `Dial` (outbound INVITE), `AnswerIncoming`
//! (inbound INVITE), and BYE.

use std::sync::atomic::{AtomicI64, Ordering};

use super::endpoint::{Endpoint, IncomingRequest};
use super::message::{
    build_ack, build_bye, build_invite, find_sub, trim_space_bytes, DialogParams, InviteParams,
    Response,
};
use super::Error;
use crate::gostd::net::local_host;
use crate::gostd::strconv::{go_atoi_bytes, go_quote_bytes as quote};
use crate::gostd::text::{go_fields_bytes, go_lossy};
use crate::util::rand_hex;

/// The dynamic RTP payload type used for L16/16000 in every offer/answer built here (a fixed
/// simplification: both ends of every call are code this project controls).
pub const DAHUA_PAYLOAD_TYPE: i64 = 96;

/// The minimal audio-only SDP: one `c=` line, one `m=audio` line.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct SdpInfo {
    pub host: String,
    pub port: i64,
    pub payload_type: i64,
}

/// `ParseSDP`: the connection address and the first `m=audio` line's port and payload type.
/// Later `c=` / `m=audio` lines override earlier ones.
pub fn parse_sdp(body: &[u8]) -> Result<SdpInfo, Error> {
    let mut info = SdpInfo::default();
    // line by line over the bytes, so an error text can quote an invalid byte as `\xNN` (R2-112)
    for raw in body.split(|&c| c == b'\n') {
        let raw = raw.strip_suffix(b"\r").unwrap_or(raw);
        let line = go_lossy(raw);
        let line = line.as_str();
        if let Some(rest) = line.strip_prefix("c=IN IP4 ") {
            info.host = rest.trim().to_string();
        } else if line.starts_with("m=audio ") {
            let fields: Vec<&str> = line.split_whitespace().collect();
            if fields.len() < 4 {
                return Err(Error::msg(format!(
                    "sip: malformed m=audio line {}",
                    quote(raw)
                )));
            }
            // the same fields over the bytes, for the quoted error text
            let raw_fields = go_fields_bytes(raw);
            info.port = go_atoi_bytes(raw_fields[1]).map_err(|e| {
                Error::msg(format!(
                    "sip: malformed audio port in {}: {}",
                    quote(raw),
                    e
                ))
            })?;
            info.payload_type = go_atoi_bytes(raw_fields[3]).map_err(|e| {
                Error::msg(format!(
                    "sip: malformed payload type in {}: {}",
                    quote(raw),
                    e
                ))
            })?;
        }
    }
    if info.host.is_empty() || info.port == 0 {
        return Err(Error::msg(
            "sip: SDP missing connection address or audio port",
        ));
    }
    Ok(info)
}

/// `BuildSDP`: an audio-only SDP offering/answering with `our_host:our_port` using the dahua_sip
/// profile's codec (16 kHz little-endian L16, 40 ms ptime).
pub fn build_sdp(our_host: &str, our_port: i64) -> String {
    use std::fmt::Write as _;
    let mut b = String::from("v=0\r\n");
    let _ = write!(b, "o=portico 0 0 IN IP4 {our_host}\r\n");
    b.push_str("s=portico\r\n");
    let _ = write!(b, "c=IN IP4 {our_host}\r\n");
    b.push_str("t=0 0\r\n");
    let _ = write!(b, "m=audio {our_port} RTP/AVP {DAHUA_PAYLOAD_TYPE}\r\n");
    let _ = write!(b, "a=rtpmap:{DAHUA_PAYLOAD_TYPE} L16/16000\r\n");
    b.push_str("a=ptime:40\r\n");
    b
}

/// One established SIP dialog: enough state to send BYE later and to know the peer's RTP address.
pub struct Call {
    endpoint: Endpoint,
    /// The Call-ID as text (lossy) for [`Call::call_id`]; `call_id_raw` is what goes on the wire.
    call_id: String,
    /// The identifiers taken from the peer's headers are the bytes received (R1-4): the BYE
    /// echoes them, and a station matches it to its dialog by byte.
    call_id_raw: Vec<u8>,
    local_tag: String,
    remote_tag: Vec<u8>,
    local_user: Vec<u8>,
    /// The peer's user part when we answered (UAS side); empty for a dialled call.
    remote_user: Vec<u8>,
    cseq: AtomicI64,
    pub remote_sdp: SdpInfo,
}

impl Call {
    /// This dialog's own Call-ID header value.
    pub fn call_id(&self) -> &str {
        &self.call_id
    }

    /// `Bye`: ends the dialog and waits for the answer (up to the send timeout). Takes `&self`
    /// (the CSeq is atomic) so a hangup from IPC and a remote-BYE handler can share the call
    /// without a lock held across the network wait.
    pub async fn bye(&self) -> Result<(), Error> {
        let cseq = self.cseq.fetch_add(1, Ordering::SeqCst) + 1;
        let branch = format!("z9hG4bK{}", rand_hex(8));
        let raw = build_bye(&DialogParams {
            host: self.endpoint.host().to_string(),
            local_addr: self.endpoint.local_addr().to_string(),
            from_user: self.local_user.clone(),
            to_user: self.remote_user.clone(),
            call_id: self.call_id_raw.clone(),
            from_tag: self.local_tag.clone().into_bytes(),
            to_tag: self.remote_tag.clone(),
            cseq,
            branch: branch.clone(),
        });
        self.endpoint.send_request(&raw, &branch, None).await?;
        Ok(())
    }
}

/// `Dial`: places an outbound INVITE offering `our_media` as where we want RTP and waits for a
/// final response. `callee_user` is the user part of the device's own AOR. `on_ringing` runs
/// inline for each 180 and must not block.
pub async fn dial(
    endpoint: &Endpoint,
    our_user: &str,
    callee_user: &str,
    our_media: SdpInfo,
    mut on_ringing: Option<&mut (dyn FnMut() + Send)>,
) -> Result<Call, Error> {
    let call_id = format!("{}@{}", rand_hex(8), local_host(endpoint.local_addr()));
    let from_tag = rand_hex(8);
    let branch = format!("z9hG4bK{}", rand_hex(8));
    let sdp = build_sdp(&our_media.host, our_media.port);

    let raw = build_invite(&InviteParams {
        host: endpoint.host().to_string(),
        local_addr: endpoint.local_addr().to_string(),
        from_user: our_user.into(),
        to_user: callee_user.into(),
        call_id: call_id.clone(),
        from_tag: from_tag.clone(),
        cseq: 1,
        branch: branch.clone(),
        sdp_body: sdp,
    });

    let mut cb = |p: &Response| {
        if p.status_code == 180 {
            if let Some(f) = on_ringing.as_mut() {
                f();
            }
        }
    };
    let resp = endpoint.send_request(&raw, &branch, Some(&mut cb)).await?;
    if resp.status_code != 200 {
        return Err(Error::msg(format!(
            "sip: INVITE failed: {} {}",
            resp.status_code, resp.reason
        )));
    }
    let remote_sdp = parse_sdp(&resp.body)
        .map_err(|e| Error::msg(format!("sip: parsing INVITE 200 OK SDP: {e}")))?;
    let remote_tag = tag_from_header_raw(resp.header_raw("To"));

    let ack = build_ack(&DialogParams {
        host: endpoint.host().to_string(),
        local_addr: endpoint.local_addr().to_string(),
        from_user: our_user.as_bytes().to_vec(),
        to_user: callee_user.as_bytes().to_vec(),
        call_id: call_id.clone().into_bytes(),
        from_tag: from_tag.clone().into_bytes(),
        to_tag: remote_tag.clone(),
        cseq: 1,
        branch: format!("z9hG4bK{}", rand_hex(8)),
    });
    endpoint
        .write(&ack)
        .await
        .map_err(|e| Error::msg(format!("sip: sending ACK: {e}")))?;

    Ok(Call {
        endpoint: endpoint.clone(),
        call_id_raw: call_id.clone().into_bytes(),
        call_id,
        local_tag: from_tag,
        remote_tag,
        local_user: our_user.as_bytes().to_vec(),
        remote_user: Vec::new(),
        cseq: AtomicI64::new(1),
        remote_sdp,
    })
}

/// `AnswerIncoming`: answers an inbound INVITE with a 200 OK offering `our_media` as where we
/// want RTP, and returns the resulting [`Call`]. (The caller sends any 180 itself.)
pub async fn answer_incoming(req: &IncomingRequest, our_media: SdpInfo) -> Result<Call, Error> {
    let remote_sdp =
        parse_sdp(&req.body).map_err(|e| Error::msg(format!("sip: parsing INVITE SDP: {e}")))?;
    let to_tag = if req.to_tag.is_empty() {
        rand_hex(8)
    } else {
        req.to_tag.clone()
    };
    let sdp = build_sdp(&our_media.host, our_media.port);
    // Checked before the 200 goes out: a request that did not arrive on an endpoint gets no
    // answer at all (`respond` would refuse it too).
    let send_err = |e: Error| Error::msg(format!("sip: sending 200 OK: {e}"));
    let endpoint = req
        .endpoint()
        .cloned()
        .ok_or_else(|| send_err(Error::msg("sip: request has no endpoint")))?;
    req.respond(200, "OK", &to_tag, &sdp)
        .await
        .map_err(send_err)?;
    Ok(Call {
        endpoint,
        call_id: req.header("Call-ID").to_string(),
        call_id_raw: req.header_raw("Call-ID").to_vec(),
        local_tag: to_tag,
        remote_tag: tag_from_header_raw(req.header_raw("From")),
        // The INVITE's To is who they dialled, i.e. our identity in this dialog; their From is
        // the remote party. Without these the BYE goes out with an empty From user, which the
        // Dahua station drops.
        local_user: user_from_header_raw(req.header_raw("To")),
        remote_user: user_from_header_raw(req.header_raw("From")),
        cseq: AtomicI64::new(0),
        remote_sdp,
    })
}

/// The user part of the SIP URI in a From/To value as received (`<sip:9901#0@host>;tag=x`,
/// `sip:u@h`, `"Name" <sip:u@h>`), bytes untouched.
pub fn user_from_header_raw(header: &[u8]) -> Vec<u8> {
    let Some(i) = find_sub(header, b"sip:") else {
        return Vec::new();
    };
    let mut rest = &header[i + 4..];
    if let Some(end) = rest.iter().position(|&c| c == b'>' || c == b';') {
        rest = &rest[..end];
    }
    match rest.iter().position(|&c| c == b'@') {
        Some(at) => trim_space_bytes(&rest[..at]).to_vec(),
        None => Vec::new(),
    }
}

/// The `tag=` parameter of a From/To value as received (empty if none), bytes untouched.
pub fn tag_from_header_raw(header: &[u8]) -> Vec<u8> {
    let Some(idx) = find_sub(header, b"tag=") else {
        return Vec::new();
    };
    let mut rest = &header[idx + 4..];
    if let Some(semi) = rest.iter().position(|&c| c == b';') {
        rest = &rest[..semi];
    }
    trim_space_bytes(rest).to_vec()
}

/// The user part of the SIP URI in a From/To value (`<sip:9901#0@host>;tag=x`, `sip:u@h`,
/// `"Name" <sip:u@h>`).
pub fn user_from_header(header: &str) -> String {
    go_lossy(&user_from_header_raw(header.as_bytes()))
}

/// The `tag=` parameter of a From/To value (`""` if none).
pub fn tag_from_header(header: &str) -> String {
    go_lossy(&tag_from_header_raw(header.as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
    use std::time::Duration;
    use tokio::net::UdpSocket;
    use tokio::sync::mpsc;
    use tokio::time::timeout;

    use crate::sip::endpoint::tests::via_line;

    // TestDial_OutboundInviteAckBye
    #[tokio::test]
    async fn dial_outbound_invite_ack_bye() {
        let conn = Arc::new(UdpSocket::bind("127.0.0.1:0").await.unwrap());
        let (ack_tx, mut ack_rx) = mpsc::unbounded_channel::<()>();
        let (bye_tx, mut bye_rx) = mpsc::unbounded_channel::<()>();
        let c2 = conn.clone();
        tokio::spawn(async move {
            let mut buf = vec![0u8; 4096];
            while let Ok((n, raddr)) = c2.recv_from(&mut buf).await {
                let req = String::from_utf8_lossy(&buf[..n]).into_owned();
                let via = via_line(&req);
                if req.starts_with("INVITE ") {
                    let ringing = format!("SIP/2.0 180 Ringing\r\n{via}Content-Length: 0\r\n\r\n");
                    let _ = c2.send_to(ringing.as_bytes(), raddr).await;
                    let sdp = build_sdp("127.0.0.1", 45000);
                    let ok = format!(
                        "SIP/2.0 200 OK\r\n{via}To: <sip:device@127.0.0.1>;tag=devicetag\r\nContent-Type: application/sdp\r\nContent-Length: {}\r\n\r\n{sdp}",
                        sdp.len()
                    );
                    let _ = c2.send_to(ok.as_bytes(), raddr).await;
                } else if req.starts_with("ACK ") {
                    let _ = ack_tx.send(());
                } else if req.starts_with("BYE ") {
                    let _ = bye_tx.send(());
                    let resp = format!("SIP/2.0 200 OK\r\n{via}Content-Length: 0\r\n\r\n");
                    let _ = c2.send_to(resp.as_bytes(), raddr).await;
                }
            }
        });
        let addr = conn.local_addr().unwrap();
        let endpoint = Endpoint::new(&addr.ip().to_string(), i64::from(addr.port()))
            .await
            .unwrap();

        let rang = Arc::new(AtomicBool::new(false));
        let r2 = rang.clone();
        let mut on_ring = move || r2.store(true, Ordering::SeqCst);
        let call = dial(
            &endpoint,
            "portico",
            "9901#0",
            SdpInfo {
                host: "127.0.0.1".into(),
                port: 40000,
                payload_type: 0,
            },
            Some(&mut on_ring),
        )
        .await
        .unwrap();
        assert!(
            rang.load(Ordering::SeqCst),
            "onRinging never fired for the 180 Ringing response"
        );
        assert_eq!(call.remote_sdp.host, "127.0.0.1");
        assert_eq!(call.remote_sdp.port, 45000);
        timeout(Duration::from_secs(2), ack_rx.recv())
            .await
            .expect("device never received an ACK after 200 OK");
        call.bye().await.unwrap();
        timeout(Duration::from_secs(2), bye_rx.recv())
            .await
            .expect("device never received a BYE");
        endpoint.close().unwrap();
    }

    // TestAnswerIncoming_RingsAndAnswers
    #[tokio::test]
    async fn answer_incoming_rings_and_answers() {
        let peer = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let peer_addr = peer.local_addr().unwrap();
        let endpoint = Endpoint::new(&peer_addr.ip().to_string(), i64::from(peer_addr.port()))
            .await
            .unwrap();
        let (tx, mut rx) = mpsc::unbounded_channel::<IncomingRequest>();
        endpoint.handle("INVITE", move |req| {
            let tx = tx.clone();
            async move {
                let _ = tx.send(req);
            }
        });

        let offer = build_sdp("127.0.0.1", 41000);
        let invite = format!(
            "INVITE sip:portico@{la} SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{pp};branch=z9hG4bKdevicebranch\r\nFrom: <sip:9901#0@127.0.0.1>;tag=devicetag\r\nTo: <sip:portico@{la}>\r\nCall-ID: ring-call-1@127.0.0.1\r\nCSeq: 1 INVITE\r\nContent-Type: application/sdp\r\nContent-Length: {len}\r\n\r\n{offer}",
            la = endpoint.local_addr(),
            pp = peer_addr.port(),
            len = offer.len()
        );
        let target: std::net::SocketAddr = endpoint.local_addr().parse().unwrap();
        peer.send_to(invite.as_bytes(), target).await.unwrap();

        let req = timeout(Duration::from_secs(2), rx.recv())
            .await
            .expect("endpoint never dispatched the inbound INVITE")
            .unwrap();
        let call = answer_incoming(
            &req,
            SdpInfo {
                host: "127.0.0.1".into(),
                port: 42000,
                payload_type: 0,
            },
        )
        .await
        .unwrap();
        assert_eq!(call.remote_sdp.port, 41000, "from the offer");

        let mut buf = vec![0u8; 4096];
        let n = timeout(Duration::from_secs(2), peer.recv(&mut buf))
            .await
            .expect("peer never received the 200 OK")
            .unwrap();
        let resp = super::super::message::parse_response(&buf[..n]).unwrap();
        assert_eq!(resp.status_code, 200);
        let answer = parse_sdp(&resp.body).unwrap();
        assert_eq!(answer.port, 42000, "our own media target");

        // GO-7: the UAS-side BYE must carry From = our To identity + our tag, To = their From
        // identity + their tag (RFC 3261 12.2.1.1).
        let local_tag = call.local_tag.clone();
        let bye = tokio::spawn(async move {
            let _ = call.bye().await;
        });
        let n = timeout(Duration::from_secs(2), peer.recv(&mut buf))
            .await
            .expect("peer never received the BYE")
            .unwrap();
        let bye_text = String::from_utf8_lossy(&buf[..n]).into_owned();
        assert!(
            bye_text.starts_with("BYE "),
            "expected BYE, got {bye_text:?}"
        );
        let (mut from, mut to) = (String::new(), String::new());
        for line in bye_text.split("\r\n") {
            if let Some(v) = line.strip_prefix("From: ") {
                from = v.into();
            } else if let Some(v) = line.strip_prefix("To: ") {
                to = v.into();
            }
        }
        bye.abort(); // the BYE waits for an answer that never comes
        assert_eq!(user_from_header(&from), "portico");
        assert_eq!(tag_from_header(&from), local_tag);
        assert_eq!(user_from_header(&to), "9901#0");
        assert_eq!(tag_from_header(&to), "devicetag");
        endpoint.close().unwrap();
    }

    #[test]
    fn sdp_roundtrip_and_errors() {
        let s = build_sdp("10.0.0.1", 4000);
        let info = parse_sdp(s.as_bytes()).unwrap();
        assert_eq!(
            info,
            SdpInfo {
                host: "10.0.0.1".into(),
                port: 4000,
                payload_type: 96
            }
        );
        assert!(parse_sdp(b"").is_err());
        assert!(parse_sdp(b"c=IN IP4 1.1.1.1\r\nm=audio 5 RTP/AVP\r\n").is_err());
    }

    #[test]
    fn header_pickers() {
        assert_eq!(user_from_header("<sip:9901#0@host>;tag=x"), "9901#0");
        assert_eq!(user_from_header("\"N\" <sip:u@h>"), "u");
        assert_eq!(user_from_header("sip:nohost"), "");
        assert_eq!(tag_from_header("<sip:a@b>;tag=abc;x=y"), "abc");
        assert_eq!(tag_from_header("<sip:a@b>"), "");
    }
}
