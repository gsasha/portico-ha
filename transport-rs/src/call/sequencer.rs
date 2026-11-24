//! `sequencer.go`: the never-dial-before-the-client-leg-lands rule (design doc 9.3, 9.6) as an
//! ordering primitive. The device is answered only when the client leg reports Connected, and
//! only then are the two audio directions joined.

use std::future::Future;
use std::sync::Arc;

use tokio::sync::{mpsc, oneshot, Mutex};

use super::{ClientLeg, Relay};
use crate::driver::TalkbackSink;

/// The device side of a call once it is answered: Go's `relay.AudioIn` / `relay.AudioOut`.
#[derive(Clone)]
pub struct DeviceLeg {
    /// frames to the device
    pub input: mpsc::Sender<Vec<u8>>,
    /// frames from the device
    pub output: Arc<Mutex<mpsc::Receiver<Vec<u8>>>>,
}

/// `forward`: copies frames from `src` to `dst` until `src` closes; a full `dst` drops the new
/// frame (a stale audio frame is worse than a dropped one), as every other leg does.
pub async fn forward(src: Arc<Mutex<mpsc::Receiver<Vec<u8>>>>, dst: mpsc::Sender<Vec<u8>>) {
    loop {
        let frame = src.lock().await.recv().await;
        match frame {
            None => return,
            Some(f) => {
                let _ = dst.try_send(f);
            }
        }
    }
}

/// `AnswerViaRelayOnceConnected`, with the answer step (`relay.AnswerDevice` in Go) passed in.
///
/// Defers `answer` until `leg` reports Connected, then joins both legs' audio: the device's
/// audio flows to the leg and the leg's audio to the device, two forwarding tasks that run until
/// their source closes. Returns at once; the receiver carries the outcome (`Ok` once the audio is
/// joined, the wrapped error of `answer` otherwise) and never resolves if the leg never
/// connects: a caller wanting a timeout owns it, this only orders two events.
///
/// The answer runs on a task of its own: the connected callback fires on the session's task and
/// must not wait for a multi-second SIP negotiation.
pub fn answer_once_connected<F, Fut>(
    leg: Arc<dyn ClientLeg>,
    answer: F,
) -> oneshot::Receiver<Result<(), String>>
where
    F: FnOnce() -> Fut + Send + 'static,
    Fut: Future<Output = Result<DeviceLeg, String>> + Send + 'static,
{
    let (tx, rx) = oneshot::channel();
    let l = leg.clone();
    leg.on_connected(Box::new(move || {
        tokio::spawn(async move {
            match answer().await {
                Err(e) => {
                    let _ = tx.send(Err(format!(
                        "call: answering device leg after client connected: {e}"
                    )));
                }
                Ok(dev) => {
                    tokio::spawn(forward(dev.output, l.input()));
                    tokio::spawn(forward(l.output(), dev.input));
                    let _ = tx.send(Ok(()));
                }
            }
        });
    }));
    rx
}

/// `OpenTalkbackOnceConnected`: the talkback counterpart of [`answer_once_connected`]: the
/// device-side path (`open` reports its outcome) is not opened until `leg` reports Connected,
/// then the leg's audio is forwarded into it. Same contract otherwise: returns at once, the
/// receiver carries `open`'s outcome (wrapped the way Go wraps it), no timeout is owned here. The
/// relay takes the sink over ([`Relay::use_talkback`]).
pub fn open_talkback_once_connected<F, Fut>(
    relay: Arc<Relay>,
    open: F,
    leg: Arc<dyn ClientLeg>,
) -> oneshot::Receiver<Result<(), String>>
where
    F: FnOnce() -> Fut + Send + 'static,
    Fut: Future<Output = Result<Arc<dyn TalkbackSink>, String>> + Send + 'static,
{
    let (tx, rx) = oneshot::channel();
    let l = leg.clone();
    leg.on_connected(Box::new(move || {
        tokio::spawn(async move {
            match open().await {
                Err(e) => {
                    let _ = tx.send(Err(format!(
                        "call: opening talkback after client connected: {e}"
                    )));
                }
                Ok(sink) => {
                    let input = sink.input();
                    relay.use_talkback(sink);
                    tokio::spawn(forward(l.output(), input));
                    let _ = tx.send(Ok(()));
                }
            }
        });
    }));
    rx
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Mutex as StdMutex;
    use std::time::Duration;

    /// `fakeClientLeg`: the test controls exactly when "connected" fires.
    struct FakeLeg {
        cb: StdMutex<Option<Box<dyn FnOnce() + Send>>>,
        connected: AtomicBool,
        in_tx: mpsc::Sender<Vec<u8>>,
        in_rx: Arc<Mutex<mpsc::Receiver<Vec<u8>>>>,
        out_tx: mpsc::Sender<Vec<u8>>,
        out_rx: Arc<Mutex<mpsc::Receiver<Vec<u8>>>>,
    }

    impl FakeLeg {
        fn new() -> Arc<FakeLeg> {
            let (in_tx, in_rx) = mpsc::channel(8);
            let (out_tx, out_rx) = mpsc::channel(8);
            Arc::new(FakeLeg {
                cb: StdMutex::new(None),
                connected: AtomicBool::new(false),
                in_tx,
                in_rx: Arc::new(Mutex::new(in_rx)),
                out_tx,
                out_rx: Arc::new(Mutex::new(out_rx)),
            })
        }

        fn trigger_connected(&self) {
            self.connected.store(true, Ordering::SeqCst);
            let cb = self.cb.lock().unwrap().take();
            if let Some(cb) = cb {
                cb();
            }
        }
    }

    impl ClientLeg for FakeLeg {
        fn on_connected(&self, cb: Box<dyn FnOnce() + Send>) {
            if self.connected.load(Ordering::SeqCst) {
                cb();
            } else {
                *self.cb.lock().unwrap() = Some(cb);
            }
        }
        fn on_disconnected(&self, _cb: Box<dyn FnOnce() + Send>) {}
        fn input(&self) -> mpsc::Sender<Vec<u8>> {
            self.in_tx.clone()
        }
        fn output(&self) -> Arc<Mutex<mpsc::Receiver<Vec<u8>>>> {
            self.out_rx.clone()
        }
    }

    // TestAnswerViaRelayOnceConnected_DefersUntilConnectedThenJoinsAudio: the fake device is a
    // pair of channels and an `answered` flag instead of a SIP socket (the relay itself is `relay.rs`).
    #[tokio::test]
    async fn answer_via_relay_once_connected_defers_until_connected_then_joins_audio() {
        let (to_dev_tx, to_dev_rx) = mpsc::channel::<Vec<u8>>(8);
        let (from_dev_tx, from_dev_rx) = mpsc::channel::<Vec<u8>>(8);
        let answered = Arc::new(AtomicBool::new(false));
        let a2 = answered.clone();
        let dev_out = Arc::new(Mutex::new(from_dev_rx));
        let leg = FakeLeg::new();
        let result = answer_once_connected(leg.clone(), move || async move {
            a2.store(true, Ordering::SeqCst);
            Ok(DeviceLeg {
                input: to_dev_tx,
                output: dev_out,
            })
        });
        // The device must NOT have been answered yet.
        tokio::time::sleep(Duration::from_millis(300)).await;
        assert!(
            !answered.load(Ordering::SeqCst),
            "device was answered before the client leg reported Connected"
        );
        leg.trigger_connected();
        let r = tokio::time::timeout(Duration::from_secs(2), result)
            .await
            .expect("timed out waiting for the deferred answer to complete")
            .unwrap();
        assert_eq!(r, Ok(()));
        assert!(answered.load(Ordering::SeqCst));
        // real audio, both directions, through the join
        let device_frame = vec![0x55u8; 1280];
        from_dev_tx.send(device_frame.clone()).await.unwrap();
        let got = tokio::time::timeout(Duration::from_secs(2), leg.in_rx.lock().await.recv())
            .await
            .expect("device audio never reached the client leg")
            .unwrap();
        assert_eq!(got, device_frame);
        let leg_frame = vec![0x66u8; 1280];
        leg.out_tx.send(leg_frame.clone()).await.unwrap();
        let mut to_dev_rx = to_dev_rx;
        let got = tokio::time::timeout(Duration::from_secs(2), to_dev_rx.recv())
            .await
            .expect("device never received the client leg's audio")
            .unwrap();
        assert_eq!(got, leg_frame);
    }

    #[tokio::test]
    async fn a_failed_answer_is_reported_wrapped_and_joins_nothing() {
        let leg = FakeLeg::new();
        let result = answer_once_connected(leg.clone(), || async { Err("no ring".to_string()) });
        leg.trigger_connected();
        let r = result.await.unwrap();
        assert_eq!(
            r,
            Err("call: answering device leg after client connected: no ring".to_string())
        );
    }

    #[tokio::test]
    async fn a_leg_that_is_already_connected_answers_at_once() {
        let leg = FakeLeg::new();
        leg.trigger_connected();
        let (tx, _rx) = mpsc::channel(1);
        let (_t2, r2) = mpsc::channel(1);
        let result = answer_once_connected(leg, move || async move {
            Ok(DeviceLeg {
                input: tx,
                output: Arc::new(Mutex::new(r2)),
            })
        });
        assert_eq!(result.await.unwrap(), Ok(()));
    }

    #[tokio::test]
    async fn forward_drops_when_the_destination_is_full_and_ends_when_the_source_closes() {
        let (src_tx, src_rx) = mpsc::channel(8);
        let (dst_tx, mut dst_rx) = mpsc::channel(2);
        let task = tokio::spawn(forward(Arc::new(Mutex::new(src_rx)), dst_tx));
        for i in 0..5u8 {
            src_tx.send(vec![i]).await.unwrap();
        }
        drop(src_tx);
        task.await.unwrap();
        assert_eq!(dst_rx.recv().await, Some(vec![0]));
        assert_eq!(dst_rx.recv().await, Some(vec![1]));
        assert_eq!(dst_rx.recv().await, None); // 2, 3, 4 were dropped, then the channel closed
    }

    /// `fakeTalkback` of `internal/call/talkback_test.go`.
    struct FakeTalkback {
        tx: mpsc::Sender<Vec<u8>>,
        closed: AtomicBool,
    }

    impl TalkbackSink for FakeTalkback {
        fn input(&self) -> mpsc::Sender<Vec<u8>> {
            self.tx.clone()
        }
        fn close(&self) -> Result<(), crate::driver::DriverError> {
            self.closed.store(true, Ordering::SeqCst);
            Ok(())
        }
    }

    // TestOpenTalkbackOnceConnected_OpensOnlyAfterTheClientLands
    #[tokio::test]
    async fn open_talkback_once_connected_opens_only_after_the_client_lands() {
        let leg = FakeLeg::new();
        let (tx, mut rx) = mpsc::channel::<Vec<u8>>(4);
        let tb = Arc::new(FakeTalkback {
            tx,
            closed: AtomicBool::new(false),
        });
        let opened = Arc::new(std::sync::atomic::AtomicI32::new(0));
        let relay = Arc::new(Relay::new());
        let (o2, tb2) = (opened.clone(), tb.clone());
        let result = open_talkback_once_connected(
            relay.clone(),
            move || async move {
                o2.fetch_add(1, Ordering::SeqCst);
                Ok(tb2 as Arc<dyn TalkbackSink>)
            },
            leg.clone(),
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert_eq!(
            opened.load(Ordering::SeqCst),
            0,
            "talkback opened before the client leg connected"
        );
        leg.trigger_connected();
        let r = tokio::time::timeout(Duration::from_secs(2), result)
            .await
            .expect("no result")
            .unwrap();
        assert_eq!(r, Ok(()));

        leg.out_tx.send(vec![1, 2]).await.unwrap();
        let got = tokio::time::timeout(Duration::from_secs(2), rx.recv())
            .await
            .expect("client frame never reached the talkback")
            .unwrap();
        assert_eq!(got.len(), 2);

        // Send-only: the relay's output yields nothing and is closed.
        let dev = relay.device_leg().expect("no device leg");
        let out = tokio::time::timeout(Duration::from_millis(500), dev.output.lock().await.recv())
            .await
            .expect("the output should be closed (send-only), not blocking");
        assert!(out.is_none(), "AudioOut yielded a frame");

        relay.close().await.unwrap();
        assert!(
            tb.closed.load(Ordering::SeqCst),
            "Close did not close the talkback"
        );
    }

    // TestOpenTalkbackOnceConnected_ReportsAnOpenFailure
    #[tokio::test]
    async fn open_talkback_once_connected_reports_an_open_failure() {
        let leg = FakeLeg::new();
        let result = open_talkback_once_connected(
            Arc::new(Relay::new()),
            || async { Err("no ffmpeg".to_string()) },
            leg.clone(),
        );
        leg.trigger_connected();
        let r = tokio::time::timeout(Duration::from_secs(2), result)
            .await
            .expect("no result")
            .unwrap();
        assert_eq!(
            r,
            Err("call: opening talkback after client connected: no ffmpeg".to_string())
        );
    }

    // TestOpenTalkbackOnceConnected_FullTalkbackDropsFramesWithoutBlocking: Go's talkback has an
    // unbuffered channel nobody reads; a bounded channel cannot be smaller than one, so it is
    // filled first.
    #[tokio::test]
    async fn open_talkback_once_connected_full_talkback_drops_frames_without_blocking() {
        let leg = FakeLeg::new();
        let (tx, _rx) = mpsc::channel::<Vec<u8>>(1);
        tx.send(vec![0]).await.unwrap(); // always full from here on
        let tb = Arc::new(FakeTalkback {
            tx,
            closed: AtomicBool::new(false),
        });
        let result = open_talkback_once_connected(
            Arc::new(Relay::new()),
            move || async move { Ok(tb as Arc<dyn TalkbackSink>) },
            leg.clone(),
        );
        leg.trigger_connected();
        let _ = result.await;
        for _ in 0..5 {
            leg.out_tx.send(vec![1]).await.unwrap();
        }
        let deadline = tokio::time::Instant::now() + Duration::from_secs(1);
        while leg.out_tx.capacity() < 8 && tokio::time::Instant::now() < deadline {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        assert_eq!(
            leg.out_tx.capacity(),
            8,
            "forwarder blocked on a full talkback"
        );
    }

    // Rust-only: a relay that was closed before the talkback arrived closes the sink at once
    #[tokio::test]
    async fn a_closed_relay_closes_a_talkback_that_arrives_late() {
        let relay = Relay::new();
        relay.close().await.unwrap();
        let (tx, _rx) = mpsc::channel::<Vec<u8>>(1);
        let tb = Arc::new(FakeTalkback {
            tx,
            closed: AtomicBool::new(false),
        });
        relay.use_talkback(tb.clone());
        assert!(tb.closed.load(Ordering::SeqCst));
        assert!(relay.device_leg().is_none());
    }
}
