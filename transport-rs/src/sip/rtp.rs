//! Port of `rtp.go`: the fixed 12-byte RTP header and `AudioSession`, which sends and receives
//! L16/16000 audio for one negotiated call over a UDP socket, with received audio jitter-buffered
//! (jitter.rs).

use crate::util::lock;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::net::UdpSocket;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

use super::dialog::{SdpInfo, DAHUA_PAYLOAD_TYPE};
use super::jitter::JitterBuffer;
use super::Error;
use crate::cancel::CancelToken;
use crate::gojson::io_err;
use crate::gostd::net::resolve_udp;
use crate::util::rand_u32;

const RTP_SAMPLE_RATE: usize = 16000;
const RTP_PTIME_MILLIS: u64 = 40;
/// 640 samples per frame (16000 * 0.040).
const RTP_SAMPLES_PER_PKT: u32 = (RTP_SAMPLE_RATE as u64 * RTP_PTIME_MILLIS / 1000) as u32;
/// 1280 bytes: L16 is 2 bytes per sample.
pub const RTP_FRAME_BYTES: usize = RTP_SAMPLES_PER_PKT as usize * 2;
const RTP_PAYLOAD_TYPE: u8 = DAHUA_PAYLOAD_TYPE as u8;

/// The fixed 12-byte RTP header (RFC 3550) and payload. No CSRC list, no extension on output.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct RtpPacket {
    pub payload_type: u8,
    pub sequence_number: u16,
    pub timestamp: u32,
    pub ssrc: u32,
    pub marker: bool,
    pub payload: Vec<u8>,
}

impl RtpPacket {
    pub fn marshal(&self) -> Vec<u8> {
        let mut buf = Vec::with_capacity(12 + self.payload.len());
        buf.push(0x80); // version 2, no padding, no extension, CC=0
        let mut pt = self.payload_type & 0x7f;
        if self.marker {
            pt |= 0x80;
        }
        buf.push(pt);
        buf.extend_from_slice(&self.sequence_number.to_be_bytes());
        buf.extend_from_slice(&self.timestamp.to_be_bytes());
        buf.extend_from_slice(&self.ssrc.to_be_bytes());
        buf.extend_from_slice(&self.payload);
        buf
    }
}

/// `ParseRTPPacket`. Skips the CSRC list but, like Go, ignores the padding and extension bits.
pub fn parse_rtp_packet(raw: &[u8]) -> Result<RtpPacket, Error> {
    if raw.len() < 12 {
        return Err(Error::msg(format!(
            "sip: RTP packet too short: {} bytes",
            raw.len()
        )));
    }
    let version = raw[0] >> 6;
    if version != 2 {
        return Err(Error::msg(format!(
            "sip: unsupported RTP version {version}"
        )));
    }
    let cc = usize::from(raw[0] & 0x0f);
    let header_len = 12 + cc * 4;
    if raw.len() < header_len {
        return Err(Error::msg("sip: RTP packet shorter than its own CSRC list"));
    }
    Ok(RtpPacket {
        marker: raw[1] & 0x80 != 0,
        payload_type: raw[1] & 0x7f,
        sequence_number: u16::from_be_bytes([raw[2], raw[3]]),
        timestamp: u32::from_be_bytes([raw[4], raw[5], raw[6], raw[7]]),
        ssrc: u32::from_be_bytes([raw[8], raw[9], raw[10], raw[11]]),
        payload: raw[header_len..].to_vec(),
    })
}

/// Sends and receives RTP audio for one negotiated call.
///
/// `audio_in` takes raw PCM frames to send (`RTP_FRAME_BYTES` each); the receiver from
/// [`AudioSession::take_audio_out`] yields raw PCM frames, jitter-buffered into sequence order
/// and paced at the ptime cadence, a lost or too-late packet becoming silence at its slot. It
/// ends (`recv() -> None`) when the session is closed.
pub struct AudioSession {
    /// `None` once closed: dropping the last reference unbinds the port.
    socket: Mutex<Option<Arc<UdpSocket>>>,
    port: u16,
    ssrc: u32,
    audio_in: mpsc::Sender<Vec<u8>>,
    audio_in_rx: Mutex<Option<mpsc::Receiver<Vec<u8>>>>,
    audio_out_rx: Mutex<Option<mpsc::Receiver<Vec<u8>>>>,
    jitter: JitterBuffer,
    stop: CancelToken,
    tasks: Mutex<Vec<JoinHandle<()>>>,
}

impl AudioSession {
    /// `OpenAudioSocket`: opens the local RTP socket WITHOUT a target yet (`local_port` 0 =
    /// kernel-assigned). The port is what an outbound SDP must advertise, so the caller needs it
    /// ([`AudioSession::local_port`]) before building the offer. Must run inside a tokio runtime.
    pub async fn open_audio_socket(local_port: u16) -> Result<AudioSession, Error> {
        let socket = UdpSocket::bind(("0.0.0.0", local_port))
            .await
            .map_err(|e| Error::msg(format!("sip: opening RTP socket: {}", io_err(&e))))?;
        let port = socket.local_addr().map(|a| a.port()).unwrap_or(0);
        // 2x ptime (80ms) playout delay: a small fixed latency for tolerance to network jitter
        // and limited reordering (tailnet-only design, no adaptive estimator).
        let ptime = Duration::from_millis(RTP_PTIME_MILLIS);
        let jitter = JitterBuffer::new(ptime, 2 * ptime, RTP_FRAME_BYTES, 0);
        let (audio_in, audio_in_rx) = mpsc::channel(8);
        let (out_tx, audio_out_rx) = mpsc::channel(8);
        let stop = CancelToken::new();
        let fwd = tokio::spawn(forward_jitter_out(jitter.clone(), out_tx, stop.clone()));
        Ok(AudioSession {
            socket: Mutex::new(Some(Arc::new(socket))),
            port,
            ssrc: rand_u32(),
            audio_in,
            audio_in_rx: Mutex::new(Some(audio_in_rx)),
            audio_out_rx: Mutex::new(Some(audio_out_rx)),
            jitter,
            stop,
            tasks: Mutex::new(vec![fwd]),
        })
    }

    /// `NewAudioSession`: open the socket and set the remote in one step.
    pub async fn new(local_port: u16, remote: &SdpInfo) -> Result<AudioSession, Error> {
        let s = Self::open_audio_socket(local_port).await?;
        if let Err(e) = s.set_remote(remote).await {
            let _ = s.close().await;
            return Err(e);
        }
        Ok(s)
    }

    /// `SetRemote`: points outbound RTP at the peer's negotiated media address and starts the
    /// send and receive loops. Until it is called nothing is sent (frames queue in `audio_in`,
    /// up to 8). A second call is an error (Go would start a second pair of loops).
    pub async fn set_remote(&self, remote: &SdpInfo) -> Result<(), Error> {
        let addr = resolve_udp(&remote.host, remote.port).await.map_err(|e| {
            Error::msg(format!(
                "sip: resolving RTP remote {}:{}: {}",
                remote.host, remote.port, e
            ))
        })?;
        let Some(socket) = lock(&self.socket).clone() else {
            return Err(Error::msg("sip: RTP session closed"));
        };
        let Some(rx) = lock(&self.audio_in_rx).take() else {
            return Err(Error::msg("sip: RTP remote already set"));
        };
        let send = tokio::spawn(send_loop(
            socket.clone(),
            addr,
            self.ssrc,
            rx,
            self.stop.clone(),
        ));
        let recv = tokio::spawn(recv_loop(
            socket,
            addr,
            self.jitter.clone(),
            self.stop.clone(),
        ));
        lock(&self.tasks).extend([send, recv]);
        Ok(())
    }

    /// The sender for outbound PCM frames (`AudioIn`).
    pub fn audio_in(&self) -> mpsc::Sender<Vec<u8>> {
        self.audio_in.clone()
    }

    /// The receiver of jitter-buffered PCM frames (`AudioOut`); `Some` the first time only.
    pub fn take_audio_out(&self) -> Option<mpsc::Receiver<Vec<u8>>> {
        lock(&self.audio_out_rx).take()
    }

    pub fn local_port(&self) -> u16 {
        self.port
    }

    /// Stops all tasks, closes `AudioOut` and releases the socket: the port is free again when
    /// this returns (async because it waits for the tasks that share the socket to finish; they
    /// stop at their next await point, so it is prompt). Idempotent.
    pub async fn close(&self) -> Result<(), Error> {
        self.stop.cancel();
        self.jitter.close();
        let tasks: Vec<_> = lock(&self.tasks).drain(..).collect();
        for t in tasks {
            let _ = t.await;
        }
        lock(&self.socket).take();
        // `set_remote` never ran (a call that failed before the SDP answer): the receiver would
        // stay parked here and `audio_in().send()` would block for good once its 8 slots fill
        // (R1-102).
        lock(&self.audio_in_rx).take();
        Ok(())
    }
}

impl Drop for AudioSession {
    fn drop(&mut self) {
        self.stop.cancel();
        self.jitter.close();
    }
}

/// Copies the jitter buffer's ordered, paced output to `AudioOut`; the only writer of
/// `AudioOut`, so its exit (dropping `out`) is what ends consumers.
async fn forward_jitter_out(jitter: JitterBuffer, out: mpsc::Sender<Vec<u8>>, stop: CancelToken) {
    loop {
        tokio::select! {
            _ = stop.cancelled() => return,
            f = jitter.recv() => match f {
                Some(frame) => { let _ = out.try_send(frame); } // full: dropped
                None => return,
            },
        }
    }
}

async fn send_loop(
    socket: Arc<UdpSocket>,
    remote: SocketAddr,
    ssrc: u32,
    mut rx: mpsc::Receiver<Vec<u8>>,
    stop: CancelToken,
) {
    let (mut seq, mut ts) = (0u16, 0u32);
    loop {
        let frame = tokio::select! {
            _ = stop.cancelled() => return,
            f = rx.recv() => match f { Some(f) => f, None => return },
        };
        let pkt = RtpPacket {
            payload_type: RTP_PAYLOAD_TYPE,
            sequence_number: seq,
            timestamp: ts,
            ssrc,
            payload: frame,
            ..Default::default()
        };
        seq = seq.wrapping_add(1);
        ts = ts.wrapping_add(RTP_SAMPLES_PER_PKT);
        // A failed send (ICMP unreachable while the peer restarts) drops this frame only.
        let _ = socket.send_to(&pkt.marshal(), remote).await;
    }
}

async fn recv_loop(
    socket: Arc<UdpSocket>,
    remote: SocketAddr,
    jitter: JitterBuffer,
    stop: CancelToken,
) {
    let mut buf = vec![0u8; 2048];
    loop {
        let (n, from) = tokio::select! {
            _ = stop.cancelled() => return,
            r = socket.recv_from(&mut buf) => match r {
                Ok(x) => x,
                Err(_) => {
                    // transient (the loop ends only on close)
                    tokio::select! {
                        _ = stop.cancelled() => return,
                        _ = tokio::time::sleep(Duration::from_millis(10)) => continue,
                    }
                }
            },
        };
        // The socket binds 0.0.0.0, so anything that can reach the port could inject audio into
        // the call. Only the host the peer's SDP named is accepted. The port is not compared: a
        // peer may send from another port.
        if from.ip().to_canonical() != remote.ip().to_canonical() {
            continue;
        }
        let Ok(pkt) = parse_rtp_packet(&buf[..n]) else {
            continue;
        };
        jitter.push(pkt.sequence_number, pkt.payload);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::time::{sleep, timeout};

    // TestRTPPacket_MarshalParseRoundTrip
    #[test]
    fn marshal_parse_round_trip() {
        let pkt = RtpPacket {
            payload_type: 96,
            sequence_number: 12345,
            timestamp: 987654321,
            ssrc: 0xdeadbeef,
            marker: true,
            payload: vec![0x42; RTP_FRAME_BYTES],
        };
        let raw = pkt.marshal();
        assert_eq!(raw.len(), 12 + RTP_FRAME_BYTES);
        assert_eq!(parse_rtp_packet(&raw).unwrap(), pkt);
    }

    // TestParseRTPPacket_RejectsWrongVersion
    #[test]
    fn rejects_wrong_version() {
        let mut raw = vec![0u8; 12];
        raw[0] = 0x00; // version 0, not 2
        assert!(parse_rtp_packet(&raw).is_err());
    }

    // TestParseRTPPacket_RejectsTooShort
    #[test]
    fn rejects_too_short() {
        assert!(parse_rtp_packet(&[0x80, 0x60]).is_err());
    }

    fn sdp(port: u16) -> SdpInfo {
        SdpInfo {
            host: "127.0.0.1".into(),
            port: i64::from(port),
            payload_type: 0,
        }
    }

    // TestAudioSession_BidirectionalRealUDP
    #[tokio::test]
    async fn bidirectional_real_udp() {
        // Go uses fixed ports (41000, 41001); here the kernel picks both (R1-125), so a parallel
        // run or another process cannot collide: open both sockets, then point each at the other.
        let a = AudioSession::open_audio_socket(0).await.unwrap();
        let b = AudioSession::open_audio_socket(0).await.unwrap();
        a.set_remote(&sdp(b.local_port())).await.unwrap();
        b.set_remote(&sdp(a.local_port())).await.unwrap();
        let mut a_out = a.take_audio_out().unwrap();
        let mut b_out = b.take_audio_out().unwrap();
        let frame_ab = vec![0x11u8; RTP_FRAME_BYTES];
        let frame_ba = vec![0x22u8; RTP_FRAME_BYTES];
        a.audio_in().send(frame_ab.clone()).await.unwrap();
        b.audio_in().send(frame_ba.clone()).await.unwrap();

        let got = timeout(Duration::from_secs(2), b_out.recv())
            .await
            .expect("b never received a's frame")
            .unwrap();
        assert_eq!(got, frame_ab);
        let got = timeout(Duration::from_secs(2), a_out.recv())
            .await
            .expect("a never received b's frame")
            .unwrap();
        assert_eq!(got, frame_ba);
        a.close().await.unwrap();
        b.close().await.unwrap();
    }

    // TestAudioSession_DropsRTPFromOtherSource
    #[tokio::test]
    async fn drops_rtp_from_other_source() {
        let s = AudioSession::new(0, &sdp(9)).await.unwrap(); // the remote is never written to
        let mut out = s.take_audio_out().unwrap();
        let dst: SocketAddr = ([127, 0, 0, 1], s.local_port()).into();
        let pkt = RtpPacket {
            payload_type: RTP_PAYLOAD_TYPE,
            sequence_number: 1,
            payload: vec![0x11; RTP_FRAME_BYTES],
            ..Default::default()
        }
        .marshal();

        async fn send(src: &str, dst: SocketAddr, pkt: &[u8]) {
            let c = UdpSocket::bind((src, 0))
                .await
                .unwrap_or_else(|e| panic!("cannot bind {src}: {e}"));
            c.send_to(pkt, dst).await.unwrap();
        }
        async fn non_silent(out: &mut mpsc::Receiver<Vec<u8>>, d: Duration) -> bool {
            let end = tokio::time::Instant::now() + d;
            loop {
                match timeout(end - tokio::time::Instant::now(), out.recv()).await {
                    Ok(Some(f)) if f.contains(&0x11) => return true,
                    Ok(Some(_)) => {}
                    _ => return false,
                }
            }
        }

        send("127.0.0.2", dst, &pkt).await;
        assert!(
            !non_silent(&mut out, Duration::from_millis(600)).await,
            "RTP from a non-negotiated source reached AudioOut"
        );
        send("127.0.0.1", dst, &pkt).await;
        assert!(
            non_silent(&mut out, Duration::from_secs(1)).await,
            "RTP from the negotiated source was not delivered"
        );
        s.close().await.unwrap();
    }

    // TestAudioSession_CloseIdempotent
    #[tokio::test]
    async fn close_idempotent() {
        let s = AudioSession::open_audio_socket(0).await.unwrap();
        let port = s.local_port();
        assert_eq!(s.close().await, Ok(()));
        // R2-87: the second close returns at once (not after waiting for tasks again) and the
        // port is free
        timeout(Duration::from_secs(1), s.close())
            .await
            .expect("the second close did not return")
            .unwrap();
        drop(
            tokio::net::UdpSocket::bind(("0.0.0.0", port))
                .await
                .expect("the RTP port is still held after close"),
        );
    }

    // TestAudioSession_CloseClosesAudioOutAndEndsConsumers
    #[tokio::test]
    async fn close_closes_audio_out_and_ends_consumers() {
        let rt = tokio::runtime::Handle::current();
        let before = rt.metrics().num_alive_tasks();
        const CALLS: usize = 20;
        let (done_tx, mut done_rx) = mpsc::unbounded_channel::<()>();
        for _ in 0..CALLS {
            let s = AudioSession::open_audio_socket(0).await.unwrap();
            let mut out = s.take_audio_out().unwrap();
            let tx = done_tx.clone();
            tokio::spawn(async move {
                while out.recv().await.is_some() {}
                let _ = tx.send(());
            });
            s.close().await.unwrap();
        }
        for i in 0..CALLS {
            timeout(Duration::from_secs(2), done_rx.recv())
                .await
                .unwrap_or_else(|_| panic!("consumer {i} never saw AudioOut close"));
        }
        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        while rt.metrics().num_alive_tasks() > before + 2 && tokio::time::Instant::now() < deadline
        {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        let after = rt.metrics().num_alive_tasks();
        assert!(
            after <= before + 2,
            "tasks: {before} before, {after} after {CALLS} open/close cycles"
        );
    }

    #[tokio::test]
    async fn send_loop_numbers_packets_like_go() {
        // seq and timestamp start at 0 and advance by 1 and 640 per frame.
        let peer = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let s = AudioSession::new(0, &sdp(peer.local_addr().unwrap().port()))
            .await
            .unwrap();
        for _ in 0..3 {
            s.audio_in().send(vec![1; 4]).await.unwrap();
        }
        let mut buf = vec![0u8; 2048];
        for i in 0..3u32 {
            let n = timeout(Duration::from_secs(1), peer.recv(&mut buf))
                .await
                .unwrap()
                .unwrap();
            let p = parse_rtp_packet(&buf[..n]).unwrap();
            assert_eq!(
                (
                    p.payload_type,
                    u32::from(p.sequence_number),
                    p.timestamp,
                    p.marker
                ),
                (96, i, i * 640, false)
            );
        }
    }

    // Go rtp.go Close releases the fd and port at once.
    #[tokio::test]
    async fn close_releases_the_port_immediately() {
        let s = AudioSession::new(0, &sdp(9)).await.unwrap();
        let port = s.local_port();
        assert!(
            UdpSocket::bind(("0.0.0.0", port)).await.is_err(),
            "still bound while open"
        );
        s.close().await.unwrap();
        let again = UdpSocket::bind(("0.0.0.0", port))
            .await
            .expect("port not released by close()");
        drop(again);
        assert!(s.set_remote(&sdp(1234)).await.is_err());
    }

    #[tokio::test]
    async fn send_loop_survives_a_failed_send() {
        // a closed peer makes later sends fail with ICMP errors; frames after it still flow
        let peer = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let pport = peer.local_addr().unwrap().port();
        drop(peer);
        let s = AudioSession::new(0, &sdp(pport)).await.unwrap();
        for _ in 0..5 {
            s.audio_in().send(vec![1; 4]).await.unwrap();
            sleep(Duration::from_millis(10)).await;
        }
        let peer = UdpSocket::bind(("127.0.0.1", pport)).await.unwrap();
        s.audio_in().send(vec![2; 4]).await.unwrap();
        let mut buf = vec![0u8; 2048];
        let n = timeout(Duration::from_secs(1), peer.recv(&mut buf))
            .await
            .expect("sending stopped after errors")
            .unwrap();
        assert_eq!(parse_rtp_packet(&buf[..n]).unwrap().payload, vec![2; 4]);
    }

    // R1-102: after close, AudioIn errors even when set_remote never ran.
    #[tokio::test]
    async fn close_without_set_remote_ends_audio_in() {
        let s = AudioSession::open_audio_socket(0).await.unwrap();
        let tx = s.audio_in();
        s.close().await.unwrap();
        for _ in 0..9 {
            let r = timeout(Duration::from_secs(1), tx.send(vec![0; 4]))
                .await
                .expect("send blocked on a closed session");
            if r.is_err() {
                return;
            }
        }
        panic!("send never failed");
    }
}
