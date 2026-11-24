//! The UDP reader of one bound socket: datagrams to the session task, a bounded backoff on
//! receive errors, and the end of the session after `RECV_ERROR_LIMIT` of them in a row.

use super::*;

/// What one reader task needs (one per bound socket).
pub(super) struct Reader {
    pub(super) sock: Arc<UdpSocket>,
    /// the index of the socket in `Core::socks`
    pub(super) idx: usize,
    pub(super) tx: mpsc::Sender<UdpEv>,
    pub(super) stop: CancelToken,
    pub(super) shared: Arc<Shared>,
    /// test seam: every receive fails
    #[cfg(feature = "test-seams")]
    pub(super) inject: bool,
    pub(super) backoff_max: Duration,
    pub(super) call: String,
}

impl Reader {
    pub(super) async fn run(self) {
        let Reader {
            sock: s,
            idx,
            tx,
            stop,
            shared,
            #[cfg(feature = "test-seams")]
            inject,
            backoff_max,
            call,
        } = self;
        let mut buf = vec![0u8; 2048];
        let (mut errs, mut back) = (0u32, Duration::from_millis(10));
        loop {
            let r = tokio::select! {
                biased;
                _ = stop.cancelled() => return,
                r = async {
                    #[cfg(feature = "test-seams")]
                    if inject {
                        return Err(std::io::Error::other("injected receive error"));
                    }
                    s.recv_from(&mut buf).await
                } => r,
            };
            match r {
                Ok((n, src)) => {
                    errs = 0;
                    back = Duration::from_millis(10);
                    let ev = UdpEv {
                        idx,
                        src,
                        data: buf[..n].to_vec(),
                    };
                    if tx.try_send(ev).is_err() {
                        shared.stats.udp_dropped.fetch_add(1, Ordering::Relaxed);
                    }
                }
                // ICMP unreachable shows up as an error on some systems; a persistent one must
                // not spin the runtime: back off, and give the session up after
                // RECV_ERROR_LIMIT in a row
                Err(e) => {
                    errs += 1;
                    if errs >= RECV_ERROR_LIMIT {
                        crate::panic_hook::out_line(&format!("portico-transport: sfu call={call} UDP receive failed {errs} times in a row ({e}); closing the session"));
                        stop.cancel();
                        return;
                    }
                    tokio::select! {
                        _ = stop.cancelled() => return,
                        _ = tokio::time::sleep(back) => {}
                    }
                    back = (back * 2).min(backoff_max);
                }
            }
        }
    }
}
