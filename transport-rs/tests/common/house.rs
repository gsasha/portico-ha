//! The fake legs and the fake factory of Go's `internal/house/house_test.go` (`fakeLeg`,
//! `fakeFactory`), with no network, shared by `tests/house.rs` and `tests/golden_house.rs`. A test
//! pushes uplink frames into `out` and reads the downlink from `in`.

use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::sync::{mpsc, watch, Mutex as AsyncMutex};
use tokio::time::timeout;
use transport_rs::house::{BoxFut, Factory, FramesLeg, HouseLeg, WebRtcLeg, FRAME_BYTES};
use transport_rs::wsmedia::PushError;

/// A gate that stays open once opened (Go's `close(gate)`): every waiter, present and future,
/// passes.
#[derive(Clone)]
pub struct Gate(Arc<watch::Sender<bool>>);

impl Gate {
    pub fn new() -> Gate {
        Gate(Arc::new(watch::channel(false).0))
    }
    pub fn open(&self) {
        self.0.send_replace(true);
    }
    pub async fn wait(&self) {
        let mut rx = self.0.subscribe();
        let _ = rx.wait_for(|v| *v).await;
    }
}

/// `fakeLeg`.
pub struct FakeLeg {
    pub id: String,
    pub in_tx: mpsc::Sender<Vec<u8>>,
    /// the test reads the downlink here (`l.in`)
    pub in_rx: AsyncMutex<mpsc::Receiver<Vec<u8>>>,
    /// the test pushes the uplink here (`l.out`)
    pub out_tx: mpsc::Sender<Vec<u8>>,
    pub out_rx: Arc<AsyncMutex<mpsc::Receiver<Vec<u8>>>>,
    pub st: Mutex<FakeState>,
    pub offer_started: AtomicUsize,
    pub offer_gate: Option<Gate>,
    pub offer_err: Option<String>,
    pub offer_panics: bool,
    pub close_panics: bool,
    pub output_panics: bool,
    /// the future `close` returns waits for this gate (a slow notification inside the close)
    pub close_gate: Option<Gate>,
    /// ... and for this long
    pub close_delay: Duration,
    /// the test holds the uplink's lock for the leg's whole life (R1-118: a pump that cannot get
    /// it must still end with its bridge)
    pub close_done: Option<Arc<AtomicUsize>>,
    pub held_out: Mutex<Option<tokio::sync::OwnedMutexGuard<mpsc::Receiver<Vec<u8>>>>>,
}

#[derive(Default)]
pub struct FakeState {
    pub closed: bool,
    pub close_reason: String,
    pub on_connected: Vec<Box<dyn FnOnce() + Send>>,
    pub on_disc: Vec<Box<dyn FnOnce() + Send>>,
}

impl FakeLeg {
    pub fn new(id: &str) -> FakeLeg {
        let (in_tx, in_rx) = mpsc::channel(8);
        let (out_tx, out_rx) = mpsc::channel(8);
        FakeLeg {
            id: id.to_string(),
            in_tx,
            in_rx: AsyncMutex::new(in_rx),
            out_tx,
            out_rx: Arc::new(AsyncMutex::new(out_rx)),
            st: Mutex::new(FakeState::default()),
            offer_started: AtomicUsize::new(0),
            offer_gate: None,
            offer_err: None,
            offer_panics: false,
            close_panics: false,
            output_panics: false,
            close_gate: None,
            close_delay: Duration::ZERO,
            close_done: None,
            held_out: Mutex::new(None),
        }
    }
    pub fn is_closed(&self) -> bool {
        self.st.lock().unwrap().closed
    }
    pub fn reason(&self) -> String {
        self.st.lock().unwrap().close_reason.clone()
    }
    /// `connect`: the WebRTC leg's ICE came up.
    pub fn connect(&self) {
        let cbs = std::mem::take(&mut self.st.lock().unwrap().on_connected);
        for cb in cbs {
            cb();
        }
    }
    /// The test side of `l.out <- frame`: waits for room, like a blocking channel send.
    pub async fn send_up(&self, f: Vec<u8>) {
        timeout(Duration::from_secs(2), self.out_tx.send(f))
            .await
            .expect("the uplink channel stayed full")
            .unwrap();
    }
    /// `select { case l.out <- f: default: }`
    pub fn try_up(&self, f: Vec<u8>) {
        let _ = self.out_tx.try_send(f);
    }
    /// `<-l.in` with the Go test's 2 s bound.
    pub async fn recv_down(&self) -> Vec<u8> {
        let mut rx = self.in_rx.lock().await;
        timeout(Duration::from_secs(2), rx.recv())
            .await
            .expect("no frame arrived")
            .expect("the downlink channel closed")
    }
    /// `select { case x := <-l.in: ... case <-time.After(d): }`
    pub async fn try_down(&self, d: Duration) -> Option<Vec<u8>> {
        let mut rx = self.in_rx.lock().await;
        timeout(d, rx.recv()).await.ok().flatten()
    }
    pub async fn down_len(&self) -> usize {
        self.in_rx.lock().await.len()
    }
}

impl HouseLeg for FakeLeg {
    fn input(&self) -> mpsc::Sender<Vec<u8>> {
        self.in_tx.clone()
    }
    fn output(&self) -> Arc<AsyncMutex<mpsc::Receiver<Vec<u8>>>> {
        if self.output_panics {
            panic!("fake leg: output() blew up");
        }
        self.out_rx.clone()
    }
    fn on_connected(&self, cb: Box<dyn FnOnce() + Send>) {
        self.st.lock().unwrap().on_connected.push(cb);
    }
    fn on_disconnected(&self, cb: Box<dyn FnOnce() + Send>) {
        let mut g = self.st.lock().unwrap();
        if g.closed {
            drop(g);
            cb();
            return;
        }
        g.on_disc.push(cb);
    }
    fn close(&self, reason: &str) -> BoxFut {
        let cbs = {
            let mut g = self.st.lock().unwrap();
            if g.closed {
                return Box::pin(async {});
            }
            g.closed = true;
            g.close_reason = reason.to_string();
            std::mem::take(&mut g.on_disc)
        };
        for cb in cbs {
            cb();
        }
        if self.close_panics {
            return Box::pin(async { panic!("fake leg: close() blew up") });
        }
        let (gate, delay, done) = (
            self.close_gate.clone(),
            self.close_delay,
            self.close_done.clone(),
        );
        Box::pin(async move {
            if let Some(g) = gate {
                g.wait().await;
            }
            if !delay.is_zero() {
                tokio::time::sleep(delay).await;
            }
            if let Some(d) = done {
                d.fetch_add(1, Ordering::SeqCst);
            }
        })
    }
}

impl FramesLeg for FakeLeg {
    fn push(&self, p: &[u8]) -> Result<(), PushError> {
        if p.len() != FRAME_BYTES {
            return Err(PushError::BadFrame);
        }
        if self.is_closed() {
            return Err(PushError::Closed);
        }
        let _ = self.out_tx.try_send(p.to_vec());
        Ok(())
    }
}

impl WebRtcLeg for FakeLeg {
    fn handle_offer(&self, sdp: String) -> BoxFut<Result<String, String>> {
        self.offer_started.fetch_add(1, Ordering::SeqCst);
        let (gate, err, panics) = (
            self.offer_gate.clone(),
            self.offer_err.clone(),
            self.offer_panics,
        );
        Box::pin(async move {
            if let Some(g) = gate {
                g.wait().await;
            }
            if panics {
                panic!("fake leg: handle_offer blew up");
            }
            match err {
                Some(e) => Err(e),
                None => Ok(format!("answer-for:{sdp}")),
            }
        })
    }
}

/// `fakeFactory`.
#[derive(Default)]
pub struct FakeFactory {
    pub st: Mutex<FactoryState>,
}

#[derive(Default)]
pub struct FactoryState {
    pub legs: HashMap<String, Arc<FakeLeg>>,
    pub order: Vec<Arc<FakeLeg>>,
    /// side suffix whose construction fails
    pub fail_on: String,
    pub gate: HashMap<String, Gate>,
    pub build_gate: Option<Gate>,
    /// when non-empty, only the legs whose id ends with it wait on `build_gate`
    pub build_gate_suffix: String,
    pub offer_err: Option<String>,
    pub born_closed: bool,
    pub offer_panics: bool,
    pub close_panics: bool,
    pub output_panics: bool,
    pub close_gate: Option<Gate>,
    pub close_delay: Duration,
    pub hold_output: bool,
    /// counts the leg closes that ran to their end (R1-34)
    pub close_done: Option<Arc<AtomicUsize>>,
}

impl FakeFactory {
    pub async fn make(&self, id: &str) -> Result<Arc<FakeLeg>, String> {
        let bg = {
            let g = self.st.lock().unwrap();
            let hit = g.build_gate_suffix.is_empty() || id.ends_with(&g.build_gate_suffix);
            g.build_gate.clone().filter(|_| hit)
        };
        if let Some(g) = bg {
            g.wait().await; // a slow factory: the open is mid-build
        }
        let mut g = self.st.lock().unwrap();
        if !g.fail_on.is_empty() && id.ends_with(&format!("-{}", g.fail_on)) {
            return Err("factory says no".to_string());
        }
        let mut l = FakeLeg::new(id);
        l.offer_err = g.offer_err.clone();
        l.offer_gate = g.gate.get(id).cloned();
        l.offer_panics = g.offer_panics;
        l.close_panics = g.close_panics;
        l.output_panics = g.output_panics;
        l.close_gate = g.close_gate.clone();
        l.close_delay = g.close_delay;
        l.close_done = g.close_done.clone();
        if g.hold_output {
            let guard = l
                .out_rx
                .clone()
                .try_lock_owned()
                .expect("a new leg's uplink is free");
            *l.held_out.lock().unwrap() = Some(guard);
        }
        let l = Arc::new(l);
        g.legs.insert(id.to_string(), l.clone());
        g.order.push(l.clone());
        let born_closed = g.born_closed;
        drop(g);
        if born_closed {
            drop(HouseLeg::close(&*l, "gone"));
        }
        Ok(l)
    }
    pub fn built(&self) -> usize {
        self.st.lock().unwrap().order.len()
    }
    pub fn all(&self) -> Vec<Arc<FakeLeg>> {
        self.st.lock().unwrap().order.clone()
    }
    pub fn leg(&self, id: &str) -> Arc<FakeLeg> {
        self.st
            .lock()
            .unwrap()
            .legs
            .get(id)
            .cloned()
            .expect("no such leg")
    }
}

/// The factory the manager owns; the test keeps the other `Arc` to look inside.
pub struct Ff(pub Arc<FakeFactory>);

impl Factory for Ff {
    fn frames(&self, id: &str, _codec: &str) -> BoxFut<Result<Arc<dyn FramesLeg>, String>> {
        let (f, id) = (self.0.clone(), id.to_string());
        Box::pin(async move { f.make(&id).await.map(|l| l as Arc<dyn FramesLeg>) })
    }
    fn webrtc(&self, id: &str) -> BoxFut<Result<Arc<dyn WebRtcLeg>, String>> {
        let (f, id) = (self.0.clone(), id.to_string());
        Box::pin(async move { f.make(&id).await.map(|l| l as Arc<dyn WebRtcLeg>) })
    }
}
