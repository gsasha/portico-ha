//! R2-19 / R2-22: what a host that can reach the session's UDP ports can do to a live call.
//!
//! * A credential-free datagram flood at a session with SEVERAL local addresses must not starve the
//!   device-to-phone audio (round 1 declined this with a one-address test: with one socket the
//!   queue runs empty between datagrams, with four it never does).
//! * A datagram from an address that is neither the nominated pair nor a remote candidate (a
//!   forged DTLS alert from a foreign socket) is refused before str0m looks at it, counted, and
//!   does not produce one log line per datagram.

mod common;

use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use common::peer::{Peer, PeerOpts};
use tokio::time::sleep;
use transport_rs::sfu::encode_l16_frame;
use transport_rs::sfu::session::{Session, SessionConfig};

/// A session that listens on `n` loopback addresses (127.0.0.1 to 127.0.0.n).
fn multi_cfg(n: u8) -> SessionConfig {
    let mut c = SessionConfig::loopback();
    c.ifaces.only = Some(
        (1..=n)
            .map(|i| IpAddr::V4(Ipv4Addr::new(127, 0, 0, i)))
            .collect(),
    );
    c
}

/// The host candidates of an answer: where the session listens.
fn candidates_of(answer: &str) -> Vec<SocketAddr> {
    answer
        .lines()
        .filter_map(|l| l.trim().strip_prefix("a=candidate:"))
        .filter_map(|l| {
            let f: Vec<&str> = l.split_whitespace().collect();
            Some(SocketAddr::new(
                f.get(4)?.parse().ok()?,
                f.get(5)?.parse().ok()?,
            ))
        })
        .collect()
}

async fn connected_pair(n: u8) -> (Arc<Session>, Peer, Vec<SocketAddr>) {
    let peer = Peer::new(PeerOpts::default()).await;
    let session = Session::with_config("flood-call", multi_cfg(n)).unwrap();
    let answer = session
        .handle_offer(&peer.offer().await)
        .await
        .expect("handle_offer");
    let ports = candidates_of(&answer);
    assert_eq!(
        ports.len(),
        usize::from(n),
        "one host candidate per address:\n{answer}"
    );
    peer.accept_answer(&answer).await.expect("accept_answer");
    assert!(peer.wait_connected(10).await, "never connected");
    let end = Instant::now() + Duration::from_secs(5);
    while !session.connected() && Instant::now() < end {
        sleep(Duration::from_millis(20)).await;
    }
    assert!(session.connected(), "the session never connected");
    (session, peer, ports)
}

// R2-19. 4 addresses, one sender thread per port, 3 s of garbage as fast as the senders can go,
// 75 device frames of 40 ms meanwhile: the phone hears them.
#[tokio::test]
async fn a_datagram_flood_on_several_addresses_does_not_starve_the_device_audio() {
    let (session, peer, ports) = connected_pair(4).await;
    let stop = Arc::new(AtomicBool::new(false));
    let sent = Arc::new(AtomicU64::new(0));
    let threads: Vec<_> = ports
        .iter()
        .map(|&to| {
            let (stop, sent) = (stop.clone(), sent.clone());
            std::thread::spawn(move || {
                let s = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
                let mut pkt = [0xABu8; 64];
                pkt[0] = 0x80; // looks like RTP
                while !stop.load(Ordering::Relaxed) {
                    if s.send_to(&pkt, to).is_ok() {
                        sent.fetch_add(1, Ordering::Relaxed);
                    }
                }
            })
        })
        .collect();
    let before = session.stats().frames_written.load(Ordering::Relaxed);
    let rx_before = peer.received().len();
    let frame = encode_l16_frame(&vec![3000i16; 640]);
    for _ in 0..75 {
        session
            .audio_in()
            .send(frame.clone())
            .await
            .expect("the session ended under the flood");
        sleep(Duration::from_millis(40)).await;
    }
    stop.store(true, Ordering::Relaxed);
    for t in threads {
        t.join().unwrap();
    }
    sleep(Duration::from_millis(500)).await;
    let written = session.stats().frames_written.load(Ordering::Relaxed) - before;
    let heard = peer.received().len() - rx_before;
    let flood = sent.load(Ordering::Relaxed);
    assert!(
        written >= 70 && heard >= 60,
        "75 device frames under a flood of {flood} datagrams: {written} written, {heard} reached the phone"
    );
    assert!(session.connected(), "the call survived the flood");
    session.close();
}

// R2-22. A forged plaintext DTLS fatal alert (record type 21, DTLS 1.2, epoch 0, level 2,
// description 40) from a socket that is no remote candidate.
#[tokio::test]
async fn datagrams_from_a_foreign_address_are_refused_and_counted() {
    let (session, peer, ports) = connected_pair(1).await;
    let foreign = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
    let alert: [u8; 15] = [21, 0xfe, 0xfd, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 2, 40];
    for _ in 0..200 {
        foreign.send_to(&alert, ports[0]).unwrap();
    }
    let end = Instant::now() + Duration::from_secs(5);
    while session.stats().udp_refused.load(Ordering::Relaxed) < 200 && Instant::now() < end {
        sleep(Duration::from_millis(20)).await;
    }
    let st = session.stats();
    // at least the 200 forged ones (a legitimate handshake record of the peer that raced its ICE
    // pair may be refused too; DTLS retransmits it)
    assert!(
        st.udp_refused.load(Ordering::Relaxed) >= 200,
        "all refused by `accepts`"
    );
    assert_eq!(
        st.input_errors.load(Ordering::Relaxed),
        0,
        "none reached the DTLS engine"
    );
    // the call is untouched: media still flows both ways
    let rx_before = peer.received().len();
    session
        .audio_in()
        .send(encode_l16_frame(&vec![3000i16; 640]))
        .await
        .unwrap();
    let end = Instant::now() + Duration::from_secs(3);
    while peer.received().len() == rx_before && Instant::now() < end {
        sleep(Duration::from_millis(20)).await;
    }
    assert!(
        peer.received().len() > rx_before,
        "the phone still hears the device"
    );
    assert!(session.connected());
    session.close();
}
