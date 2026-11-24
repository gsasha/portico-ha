//! Fakes for the talk family (slice 7b), written from the Go tests' helpers: `talkSink`,
//! `fakeMedia`, `fakeGuard` (bell-talk's `/health`), the fake `ffmpeg` scripts (`fakeFFmpeg`,
//! `fakeFFmpegBoth`) and the `rig` (a configured `unifi_protect_talk` driver over a fake console,
//! a fake guard and a fake ffmpeg). Everything is on 127.0.0.1 or in a temporary directory; a
//! real `ffmpeg` is never needed.

// Each test crate that declares `mod common;` compiles every helper here and uses a subset, so an
// item unused in one crate is not dead in the suite (R1-202: a per-consumer split is the alternative).
#![allow(dead_code)]

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicI32, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::unifiprotect::talk::{
    TalkDriver, TalkEnv, TalkMedia, TalkOpener, TalkTiming,
};
use transport_rs::driver::{BoxFuture, CallAudio, CallReasonSink, Driver, DriverError, EventSink};

use super::unifi::{wait_for, FakeProtect, TEST_KEY};

pub fn ms(n: u64) -> Duration {
    Duration::from_millis(n)
}

/// A temporary directory removed on drop.
pub struct TmpDir(pub PathBuf);

impl TmpDir {
    pub fn new(tag: &str) -> TmpDir {
        static N: AtomicU32 = AtomicU32::new(0);
        let p = std::env::temp_dir().join(format!(
            "portico-rs-{tag}-{}-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::SeqCst),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.subsec_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(&p).unwrap();
        TmpDir(p)
    }
    pub fn path(&self) -> &Path {
        &self.0
    }
    pub fn file(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
    pub fn read(&self, name: &str) -> Vec<u8> {
        std::fs::read(self.file(name)).unwrap_or_default()
    }
    pub fn write(&self, name: &str, data: &[u8]) {
        std::fs::write(self.file(name), data).unwrap();
    }
}

impl Drop for TmpDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// Writes an executable script at `path` without ever exec'ing a file this process just wrote.
///
/// `exec` of a file that some process still holds open for writing fails with ETXTBSY ("text file
/// busy"). A test that writes a script and runs it races every other thread of the binary: a
/// thread that forks between our `open` and `close` hands the write descriptor to its child until
/// that child execs, and our own exec lands in that window (it failed once with "starting
/// ffmpeg: text file busy"). So the script text goes to `path.body`, which is only ever READ (by
/// `sh`), and `path` is a symlink to one stub, written once per process and proven executable
/// before anybody uses it, that runs the body.
pub fn write_script(path: &Path, text: &str) {
    let body = format!("{}.body", path.display());
    std::fs::write(&body, text).unwrap();
    let _ = std::fs::remove_file(path);
    std::os::unix::fs::symlink(stub(), path).unwrap();
}

/// The one stub `write_script` links to: `exec sh "$0.body" "$@"` (`$0` is the symlink's path).
fn stub() -> &'static Path {
    use std::os::unix::fs::PermissionsExt;
    static STUB: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();
    STUB.get_or_init(|| {
        let p = std::env::temp_dir().join(format!(
            "portico-rs-stub-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.subsec_nanos())
                .unwrap_or(0)
        ));
        std::fs::write(&p, "#!/bin/sh\nexec /bin/sh \"$0.body\" \"$@\"\n").unwrap();
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
        // Prove it can be exec'd now: a thread that forked while the write descriptor was open
        // holds it until its child execs, so retry while the exec says "text file busy". After
        // one success nothing writes the stub again, so no later exec can fail that way.
        let mut tries = 0;
        loop {
            match std::process::Command::new(&p)
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .status()
            {
                Ok(_) => break,
                Err(e) if e.raw_os_error() == Some(libc::ETXTBSY) && tries < 500 => {
                    tries += 1;
                    std::thread::sleep(Duration::from_millis(10));
                }
                Err(e) => panic!("the script stub cannot be executed: {e}"),
            }
        }
        p
    })
}

/// `fakeFFmpeg`: records its arguments and copies stdin to `dir/stdin`.
pub fn fake_ffmpeg(dir: &TmpDir) -> String {
    let p = dir.file("ffmpeg");
    let d = dir.path().display();
    write_script(
        &p,
        &format!("#!/bin/sh\nprintf '%s\\n' \"$@\" > {d}/args\ncat > {d}/stdin\n"),
    );
    p.display().to_string()
}

/// `fakeFFmpegBoth`: stands in for ffmpeg on both legs. The uplink (stdin to stdout-less RTP)
/// records its args and stdin as `fake_ffmpeg` does. The downlink (it writes pipe:1) records
/// `args_down`, waits for `dir/go`, then writes `dir/down.pcm` and stays up like a live RTSP
/// pull; with `dir/down_fail` it prints an error naming a URL and exits instead.
pub fn fake_ffmpeg_both(dir: &TmpDir) -> String {
    let p = dir.file("ffmpeg");
    let d = dir.path().display();
    write_script(
        &p,
        &format!(
            r#"#!/bin/sh
case "$*" in
*pipe:1*)
  printf '%s\n' "$@" > {d}/args_down
  if [ -e {d}/down_fail ]; then echo "Connection to rtsp://user:s3cret@10.6.0.6:8554/doorbell failed" >&2; exit 1; fi
  while [ ! -e {d}/go ]; do sleep 0.02; done
  cat {d}/down.pcm
  exec sleep 30;;
*)
  printf '%s\n' "$@" > {d}/args
  cat > {d}/stdin;;
esac
"#
        ),
    );
    p.display().to_string()
}

/// `pcm(n, base)`: n bytes where sample k is k+base (little endian), so a frame can be checked.
pub fn pcm(n: usize, base: usize) -> Vec<u8> {
    let mut b = vec![0u8; n];
    let mut i = 0;
    while i + 1 < n {
        let v = (i / 2 + base) as u16;
        b[i] = v as u8;
        b[i + 1] = (v >> 8) as u8;
        i += 2;
    }
    b
}

/// `talkSink`: records what the driver reports, in the Go test's spelling
/// (`id:state`, `avail:device:up`), and what it says through the `CallReasonSink`.
#[derive(Default)]
pub struct TalkSink {
    events: Mutex<Vec<String>>,
    reasons: Mutex<Vec<String>>,
    ring: AtomicI32,
}

impl TalkSink {
    pub fn new() -> Arc<TalkSink> {
        Arc::new(TalkSink::default())
    }
    pub fn got(&self) -> String {
        self.events.lock().unwrap().join(",")
    }
    pub fn rings(&self) -> i32 {
        self.ring.load(Ordering::SeqCst)
    }
    /// `heard`: `device|call|kind|reason;...`
    pub fn heard(&self) -> String {
        self.reasons.lock().unwrap().join(";")
    }
}

impl EventSink for TalkSink {
    fn ringing(&self, _: &str, _: &str) {
        self.ring.fetch_add(1, Ordering::SeqCst);
    }
    fn state_changed(&self, id: &str, st: &str) {
        self.events.lock().unwrap().push(format!("{id}:{st}"));
    }
    fn availability_changed(&self, id: &str, ok: bool, _: &str) {
        self.events
            .lock()
            .unwrap()
            .push(format!("avail:{id}:{}", if ok { "up" } else { "down" }));
    }
    fn call_reason_sink(&self) -> Option<&dyn CallReasonSink> {
        Some(self)
    }
}

impl CallReasonSink for TalkSink {
    fn call_reason(&self, device_id: &str, call_id: &str, kind: &str, reason: &str) {
        self.reasons
            .lock()
            .unwrap()
            .push(format!("{device_id}|{call_id}|{kind}|{reason}"));
    }
}

/// `fakeMedia`: a talkMedia that remembers being closed.
pub struct FakeMedia {
    in_tx: mpsc::Sender<Vec<u8>>,
    pub in_rx: tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>,
    pub out_tx: mpsc::Sender<Vec<u8>>,
    out_rx: Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>>,
    done: CancelToken,
    pub closed: AtomicI32,
    pub detached: AtomicI32,
    pub attached: AtomicI32,
    pub grace: Mutex<Option<Duration>>,
}

impl FakeMedia {
    pub fn new() -> Arc<FakeMedia> {
        let (in_tx, in_rx) = mpsc::channel(4);
        let (out_tx, out_rx) = mpsc::channel(4);
        Arc::new(FakeMedia {
            in_tx,
            in_rx: tokio::sync::Mutex::new(in_rx),
            out_tx,
            out_rx: Arc::new(tokio::sync::Mutex::new(out_rx)),
            done: CancelToken::new(),
            closed: AtomicI32::new(0),
            detached: AtomicI32::new(0),
            attached: AtomicI32::new(0),
            grace: Mutex::new(None),
        })
    }
    pub fn closed(&self) -> i32 {
        self.closed.load(Ordering::SeqCst)
    }
    pub fn detached(&self) -> i32 {
        self.detached.load(Ordering::SeqCst)
    }
}

impl CallAudio for FakeMedia {
    fn input(&self) -> mpsc::Sender<Vec<u8>> {
        self.in_tx.clone()
    }
    fn output(&self) -> Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>> {
        self.out_rx.clone()
    }
    fn attach(&self) {
        self.attached.fetch_add(1, Ordering::SeqCst);
    }
    fn detach(&self) {
        self.detached.fetch_add(1, Ordering::SeqCst);
    }
}

impl TalkMedia for FakeMedia {
    fn close(&self) {
        self.closed.fetch_add(1, Ordering::SeqCst);
        self.done.cancel();
    }
    fn closed(&self) -> BoxFuture<'_, ()> {
        Box::pin(async {})
    }
    fn done(&self) -> CancelToken {
        self.done.clone()
    }
    fn set_orphan_grace(&self, d: Duration) {
        *self.grace.lock().unwrap() = Some(d);
    }
}

/// A counting opener over `FakeMedia` (`d.open = func ... { opens.Add(1); return m, nil }`),
/// switchable to fail and to wait at a gate.
pub struct FakeOpener {
    pub opens: AtomicI32,
    pub media: Mutex<Arc<FakeMedia>>,
    pub fail: Mutex<Option<DriverError>>,
    pub gate: Mutex<Option<Arc<tokio::sync::Notify>>>,
    /// call ids it was asked for
    pub asked: Mutex<Vec<String>>,
}

impl FakeOpener {
    pub fn new() -> Arc<FakeOpener> {
        Arc::new(FakeOpener {
            opens: AtomicI32::new(0),
            media: Mutex::new(FakeMedia::new()),
            fail: Mutex::new(None),
            gate: Mutex::new(None),
            asked: Mutex::new(Vec::new()),
        })
    }
    pub fn opens(&self) -> i32 {
        self.opens.load(Ordering::SeqCst)
    }
    pub fn media(&self) -> Arc<FakeMedia> {
        self.media.lock().unwrap().clone()
    }
    pub fn fail_with(&self, e: Option<DriverError>) {
        *self.fail.lock().unwrap() = e;
    }
}

impl TalkOpener for FakeOpener {
    fn open<'a>(
        &'a self,
        _ctx: &'a CancelToken,
        _env: TalkEnv,
        call_id: &'a str,
    ) -> BoxFuture<'a, Result<Arc<dyn TalkMedia>, DriverError>> {
        Box::pin(async move {
            self.opens.fetch_add(1, Ordering::SeqCst);
            self.asked.lock().unwrap().push(call_id.to_string());
            let gate = self.gate.lock().unwrap().clone();
            if let Some(g) = gate {
                g.notified().await;
            }
            if let Some(e) = self.fail.lock().unwrap().clone() {
                return Err(e);
            }
            let m: Arc<dyn TalkMedia> = self.media();
            Ok(m)
        })
    }
}

pub fn talk_cfg() -> serde_json::Map<String, Value> {
    json!({"device_id": "ha_event:front", "protect_host": "h", "protect_api_key": TEST_KEY, "camera_id": "cam"})
        .as_object()
        .unwrap()
        .clone()
}

pub fn talk_cfg_with(extra: Value) -> serde_json::Map<String, Value> {
    let mut c = talk_cfg();
    for (k, v) in extra.as_object().cloned().unwrap_or_default() {
        c.insert(k, v);
    }
    c
}

/// A hold floor low enough for the expiry tests (Go: `talkMinHold = 10ms`).
pub fn short_holds() -> TalkTiming {
    TalkTiming {
        min_hold: ms(10),
        ..TalkTiming::default()
    }
}

/// `newTalk`: a started driver with a counting opener over a fake media.
pub async fn new_talk(
    timing: TalkTiming,
) -> (
    Arc<TalkDriver>,
    Arc<TalkSink>,
    Arc<FakeOpener>,
    Arc<FakeMedia>,
) {
    let mut d = TalkDriver::default();
    d.set_timing(timing);
    d.configure(&talk_cfg()).unwrap();
    let op = FakeOpener::new();
    d.set_opener(op.clone());
    let s = TalkSink::new();
    let d = Arc::new(d);
    d.start(&CancelToken::new(), s.clone()).await.unwrap();
    let m = op.media();
    (d, s, op, m)
}

// ---- the guard ---------------------------------------------------------------------------------

/// `fakeGuard`: stands in for bell-talk's `/health`.
pub struct FakeGuard {
    pub addr: std::net::SocketAddr,
    pub body: Arc<Mutex<String>>,
    pub code: Arc<Mutex<u16>>,
    pub hits: Arc<AtomicI32>,
    /// answers after this long
    pub delay: Arc<Mutex<Duration>>,
    kill: CancelToken,
}

impl Drop for FakeGuard {
    fn drop(&mut self) {
        self.kill.cancel();
    }
}

impl FakeGuard {
    pub async fn new(body: &str) -> FakeGuard {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let g = FakeGuard {
            addr,
            body: Arc::new(Mutex::new(body.to_string())),
            code: Arc::new(Mutex::new(200)),
            hits: Arc::new(AtomicI32::new(0)),
            delay: Arc::new(Mutex::new(Duration::ZERO)),
            kill: CancelToken::new(),
        };
        let (b, c, h, dl, k) = (
            g.body.clone(),
            g.code.clone(),
            g.hits.clone(),
            g.delay.clone(),
            g.kill.clone(),
        );
        tokio::spawn(async move {
            loop {
                let mut s = tokio::select! {
                    _ = k.cancelled() => return,
                    r = listener.accept() => match r { Ok((s, _)) => s, Err(_) => return },
                };
                let (b, c, h, dl, k) = (b.clone(), c.clone(), h.clone(), dl.clone(), k.clone());
                tokio::spawn(async move {
                    let mut buf = vec![0u8; 4096];
                    let mut got = Vec::new();
                    while !got.windows(4).any(|w| w == b"\r\n\r\n") {
                        match s.read(&mut buf).await {
                            Ok(n) if n > 0 => got.extend_from_slice(&buf[..n]),
                            _ => return,
                        }
                    }
                    h.fetch_add(1, Ordering::SeqCst);
                    let d = *dl.lock().unwrap();
                    if !d.is_zero() {
                        tokio::select! { _ = k.cancelled() => return, _ = tokio::time::sleep(d) => {} }
                    }
                    let code = *c.lock().unwrap();
                    let body = b.lock().unwrap().clone();
                    let reply = format!(
                        "HTTP/1.1 {code} X\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    let _ = s.write_all(reply.as_bytes()).await;
                    let _ = s.shutdown().await;
                });
            }
        });
        g
    }
    pub fn url(&self) -> String {
        format!("http://{}", self.addr)
    }
    pub fn set_body(&self, b: &str) {
        *self.body.lock().unwrap() = b.to_string();
    }
    pub fn set_code(&self, c: u16) {
        *self.code.lock().unwrap() = c;
    }
    pub fn hits(&self) -> i32 {
        self.hits.load(Ordering::SeqCst)
    }
    pub fn close(&self) {
        self.kill.cancel();
    }
}

// ---- the rig ---------------------------------------------------------------------------------

/// `rig`: a configured talk driver over a fake console, a fake guard and a fake ffmpeg.
pub struct Rig {
    pub d: Arc<TalkDriver>,
    pub sink: Arc<TalkSink>,
    pub con: FakeProtect,
    pub guard: FakeGuard,
    pub dir: TmpDir,
}

pub struct RigOpts {
    pub timing: TalkTiming,
    pub ffmpeg: Option<String>,
    pub extra: Value,
    /// `r.d.cfg.downlinkURL = ""` after configure
    pub no_downlink: bool,
    pub gain: Option<i64>,
}

impl Default for RigOpts {
    fn default() -> Self {
        RigOpts {
            timing: TalkTiming::default(),
            ffmpeg: None,
            extra: json!({}),
            no_downlink: false,
            gain: None,
        }
    }
}

impl Rig {
    pub async fn new() -> Rig {
        Rig::with(RigOpts::default()).await
    }

    pub async fn with(o: RigOpts) -> Rig {
        let con = FakeProtect::new().await;
        let guard = FakeGuard::new(r#"{"sessionUp":false}"#).await;
        let dir = TmpDir::new("rig");
        let mut d = TalkDriver::default();
        d.set_scheme("http");
        d.set_timing(o.timing);
        d.set_ffmpeg_binary(&o.ffmpeg.unwrap_or_else(|| fake_ffmpeg_both(&dir)));
        let mut cfg = talk_cfg_with(json!({
            "protect_host": con.host(),
            "guard_url": guard.url(),
            "camera_id": "cam1",
            "downlink_url": "rtsp://127.0.0.1:1/doorbell",
        }));
        for (k, v) in o.extra.as_object().cloned().unwrap_or_default() {
            cfg.insert(k, v);
        }
        if let Some(g) = o.gain {
            cfg.insert("gain".into(), json!(g));
        }
        if o.no_downlink {
            cfg.remove("downlink_url");
        }
        d.configure(&cfg).unwrap();
        let sink = TalkSink::new();
        let d = Arc::new(d);
        d.start(&CancelToken::new(), sink.clone()).await.unwrap();
        Rig {
            d,
            sink,
            con,
            guard,
            dir,
        }
    }

    pub fn ring(&self, hold: Duration) {
        use transport_rs::driver::ExternalRinger;
        self.d.external_ring("c1", hold).unwrap();
    }

    pub fn posts(&self) -> usize {
        self.con
            .reqs()
            .iter()
            .filter(|q| q.starts_with("POST "))
            .count()
    }

    pub fn release(&self) {
        self.dir.write("go", b"");
    }

    pub async fn wait_ended(&self, id: &str) {
        let s = self.sink.clone();
        let suffix = format!("{id}:ended");
        wait_for(&format!("{suffix} reported"), move || {
            s.got().ends_with(&suffix)
        })
        .await;
    }
}

impl Drop for Rig {
    fn drop(&mut self) {
        // a rig that is dropped mid-call must not leave ffmpegs behind: the hub's context is
        // not cancelled in these tests, so stop the driver the way the hub would
        let d = self.d.clone();
        if let Ok(h) = tokio::runtime::Handle::try_current() {
            h.spawn(async move {
                let _ = d.stop().await;
            });
        }
    }
}

// ---- the hub's fakes (main_test.go, talkback_test.go) ------------------------------------------

use std::sync::atomic::AtomicBool;
use transport_rs::call::ClientLeg;
use transport_rs::driver::{
    AudioClaimer, Capabilities, MediaTarget, Sentinel, TalkbackAnswerer, TalkbackSink,
};

/// `fakeCallAudio`.
pub struct FakeCallAudio {
    pub in_tx: mpsc::Sender<Vec<u8>>,
    pub in_rx: tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>,
    pub out_tx: Mutex<Option<mpsc::Sender<Vec<u8>>>>,
    out_rx: Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>>,
    pub attached: AtomicI32,
    pub detached: AtomicI32,
}

impl FakeCallAudio {
    pub fn new() -> Arc<FakeCallAudio> {
        let (in_tx, in_rx) = mpsc::channel(8);
        let (out_tx, out_rx) = mpsc::channel(8);
        Arc::new(FakeCallAudio {
            in_tx,
            in_rx: tokio::sync::Mutex::new(in_rx),
            out_tx: Mutex::new(Some(out_tx)),
            out_rx: Arc::new(tokio::sync::Mutex::new(out_rx)),
            attached: AtomicI32::new(0),
            detached: AtomicI32::new(0),
        })
    }
    pub fn counts(&self) -> (i32, i32) {
        (
            self.attached.load(Ordering::SeqCst),
            self.detached.load(Ordering::SeqCst),
        )
    }
    pub async fn push_out(&self, f: Vec<u8>) {
        let tx = self.out_tx.lock().unwrap().clone().expect("closed");
        tx.send(f).await.unwrap();
    }
    /// `close(a.out)`: the driver closed its side.
    pub fn close_out(&self) {
        self.out_tx.lock().unwrap().take();
    }
    pub fn out_arc(&self) -> Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>> {
        self.out_rx.clone()
    }
}

impl CallAudio for FakeCallAudio {
    fn input(&self) -> mpsc::Sender<Vec<u8>> {
        self.in_tx.clone()
    }
    fn output(&self) -> Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>> {
        self.out_rx.clone()
    }
    fn attach(&self) {
        self.attached.fetch_add(1, Ordering::SeqCst);
    }
    fn detach(&self) {
        self.detached.fetch_add(1, Ordering::SeqCst);
    }
}

/// `fakeBrowserLeg`: connects when the test says so.
pub struct FakeLeg {
    pub in_tx: mpsc::Sender<Vec<u8>>,
    pub in_rx: tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>,
    pub out_tx: mpsc::Sender<Vec<u8>>,
    out_rx: Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>>,
    on_connected: Mutex<Vec<Box<dyn FnOnce() + Send>>>,
    on_disconnected: Mutex<Vec<Box<dyn FnOnce() + Send>>>,
    connected: AtomicBool,
}

impl FakeLeg {
    pub fn new() -> Arc<FakeLeg> {
        let (in_tx, in_rx) = mpsc::channel(8);
        let (out_tx, out_rx) = mpsc::channel(8);
        Arc::new(FakeLeg {
            in_tx,
            in_rx: tokio::sync::Mutex::new(in_rx),
            out_tx,
            out_rx: Arc::new(tokio::sync::Mutex::new(out_rx)),
            on_connected: Mutex::new(Vec::new()),
            on_disconnected: Mutex::new(Vec::new()),
            connected: AtomicBool::new(false),
        })
    }
    pub fn connect(&self) {
        self.connected.store(true, Ordering::SeqCst);
        let cbs: Vec<_> = self.on_connected.lock().unwrap().drain(..).collect();
        for cb in cbs {
            cb();
        }
    }
    pub fn disconnect(&self) {
        let cbs: Vec<_> = self.on_disconnected.lock().unwrap().drain(..).collect();
        for cb in cbs {
            cb();
        }
    }
    pub fn out_arc(&self) -> Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>> {
        self.out_rx.clone()
    }
}

impl ClientLeg for FakeLeg {
    fn on_connected(&self, cb: Box<dyn FnOnce() + Send>) {
        if self.connected.load(Ordering::SeqCst) {
            cb();
        } else {
            self.on_connected.lock().unwrap().push(cb);
        }
    }
    fn on_disconnected(&self, cb: Box<dyn FnOnce() + Send>) {
        self.on_disconnected.lock().unwrap().push(cb);
    }
    fn input(&self) -> mpsc::Sender<Vec<u8>> {
        self.in_tx.clone()
    }
    fn output(&self) -> Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>> {
        self.out_rx.clone()
    }
}

/// `fakeSink` of `cmd/portico-transport/talkback_test.go` / `fakeTalkback` of
/// `internal/call/talkback_test.go`.
pub struct FakeSink {
    pub tx: mpsc::Sender<Vec<u8>>,
    pub rx: tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>,
    pub closed: AtomicBool,
}

impl FakeSink {
    pub fn new(cap: usize) -> Arc<FakeSink> {
        let (tx, rx) = mpsc::channel(cap);
        Arc::new(FakeSink {
            tx,
            rx: tokio::sync::Mutex::new(rx),
            closed: AtomicBool::new(false),
        })
    }
}

impl TalkbackSink for FakeSink {
    fn input(&self) -> mpsc::Sender<Vec<u8>> {
        self.tx.clone()
    }
    fn close(&self) -> Result<(), DriverError> {
        self.closed.store(true, Ordering::SeqCst);
        Ok(())
    }
}

/// `claimant` + `ringer` + `unsupportedDriver` in one: a driver that holds one call's audio
/// (like gate_sip_observer with claims on), one ring that is answered through a talkback (like
/// unifi_protect), neither or both.
pub struct HubFake {
    pub claimed: Mutex<Option<(String, Arc<FakeCallAudio>)>>,
    pub ringing: Mutex<String>,
    pub answered: Mutex<Vec<String>>,
    pub hungup: Mutex<Vec<String>>,
    pub sink: Arc<FakeSink>,
    pub fail_open: Mutex<Option<String>>,
    pub is_talkback: bool,
    /// when set, `answer_talkback` takes the ring (as the real driver's `answering`) and waits
    pub gate: Mutex<Option<Arc<tokio::sync::Notify>>>,
}

impl HubFake {
    /// `unsupportedDriver{}`: holds nothing, answers nothing.
    pub fn plain() -> Arc<HubFake> {
        Arc::new(HubFake {
            claimed: Mutex::new(None),
            ringing: Mutex::new(String::new()),
            answered: Mutex::new(Vec::new()),
            hungup: Mutex::new(Vec::new()),
            sink: FakeSink::new(8),
            fail_open: Mutex::new(None),
            is_talkback: false,
            gate: Mutex::new(None),
        })
    }
    /// `claimant`.
    pub fn claimant(call: &str, audio: Arc<FakeCallAudio>) -> Arc<HubFake> {
        let f = HubFake::plain();
        *f.claimed.lock().unwrap() = Some((call.to_string(), audio));
        f
    }
    /// `newRinger`.
    pub fn ringer(call: &str) -> Arc<HubFake> {
        Arc::new(HubFake {
            claimed: Mutex::new(None),
            ringing: Mutex::new(call.to_string()),
            answered: Mutex::new(Vec::new()),
            hungup: Mutex::new(Vec::new()),
            sink: FakeSink::new(8),
            fail_open: Mutex::new(None),
            is_talkback: true,
            gate: Mutex::new(None),
        })
    }
    /// `r.counts()`: (answered, hung up).
    pub fn counts(&self) -> (usize, usize) {
        (
            self.answered.lock().unwrap().len(),
            self.hungup.lock().unwrap().len(),
        )
    }
}

impl Driver for HubFake {
    fn configure(&mut self, _: &serde_json::Map<String, Value>) -> Result<(), DriverError> {
        Ok(())
    }
    fn capabilities(&self) -> Capabilities {
        Capabilities::default()
    }
    fn start<'a>(
        &'a self,
        _: &'a CancelToken,
        _: Arc<dyn EventSink>,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async { Ok(()) })
    }
    fn stop(&self) -> BoxFuture<'_, Result<(), DriverError>> {
        Box::pin(async { Ok(()) })
    }
    fn dial<'a>(
        &'a self,
        _: &'a CancelToken,
        _: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async { Err(DriverError::sentinel(Sentinel::NotSupported)) })
    }
    fn answer<'a>(
        &'a self,
        _: &'a CancelToken,
        _: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async { Err(DriverError::sentinel(Sentinel::NotSupported)) })
    }
    fn open_relay<'a>(&'a self, _: &'a CancelToken) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async { Err(DriverError::sentinel(Sentinel::NotSupported)) })
    }
    fn as_audio_claimer(&self) -> Option<&dyn AudioClaimer> {
        self.claimed
            .lock()
            .unwrap()
            .is_some()
            .then_some(self as &dyn AudioClaimer)
    }
    fn as_call_hangupper(&self) -> Option<&dyn transport_rs::driver::CallHangupper> {
        (self.is_talkback || self.claimed.lock().unwrap().is_some())
            .then_some(self as &dyn transport_rs::driver::CallHangupper)
    }
    fn as_talkback_answerer(&self) -> Option<&dyn TalkbackAnswerer> {
        self.is_talkback.then_some(self as &dyn TalkbackAnswerer)
    }
}

impl AudioClaimer for HubFake {
    fn claimed_audio(&self, call_id: &str) -> Option<Arc<dyn CallAudio>> {
        let g = self.claimed.lock().unwrap();
        match g.as_ref() {
            Some((c, a)) if c == call_id => {
                let a: Arc<dyn CallAudio> = a.clone();
                Some(a)
            }
            _ => None,
        }
    }
}

impl transport_rs::driver::CallHangupper for HubFake {
    fn hangup_call(&self, call_id: &str) -> Result<(), DriverError> {
        self.hungup.lock().unwrap().push(call_id.to_string());
        Ok(())
    }
}

impl TalkbackAnswerer for HubFake {
    fn ringing_call(&self, call_id: &str) -> bool {
        *self.ringing.lock().unwrap() == call_id
    }
    fn answer_talkback<'a>(
        &'a self,
        _ctx: &'a CancelToken,
        call_id: &'a str,
    ) -> BoxFuture<'a, Result<Arc<dyn TalkbackSink>, DriverError>> {
        Box::pin(async move {
            let gate = self.gate.lock().unwrap().clone();
            if let Some(g) = gate {
                self.ringing.lock().unwrap().clear(); // answering: no longer ringing
                g.notified().await;
            }
            if let Some(e) = self.fail_open.lock().unwrap().clone() {
                return Err(DriverError::new(e));
            }
            self.answered.lock().unwrap().push(call_id.to_string());
            self.ringing.lock().unwrap().clear();
            let s: Arc<dyn TalkbackSink> = self.sink.clone();
            Ok(s)
        })
    }
}

/// `eventuallyMain`: the condition within 2 s.
pub async fn eventually(cond: impl Fn() -> bool) {
    let end = std::time::Instant::now() + Duration::from_secs(2);
    while std::time::Instant::now() < end {
        if cond() {
            return;
        }
        tokio::time::sleep(ms(5)).await;
    }
    panic!("timed out");
}
