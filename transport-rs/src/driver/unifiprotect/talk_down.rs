//! `talk_down.go`: the visitor's voice (front-door talk plan T5), and the door's diagnostics.
//!
//! `ffmpeg` pulls the go2rtc RTSP stream of the doorbell, converts it to 16 kHz little-endian L16
//! mono (the shape every leg speaks) and the reader cuts it into 1280-byte (40 ms) frames for
//! `CallAudio::output`, through a [`Pacer`]. Late audio is worse than none: while the queue is
//! full the NEW frame is dropped and counted. Frames that arrive before a client leg has
//! attached are discarded (counted as frames from the camera, not as drops). The downlink dying
//! ends the call: a call the phone can speak into but not hear must not read as healthy.
//!
//! Differences from Go (PARITY.md 134, 135): `ffmpeg`'s stderr is always read to its end (Go's
//! `bufio.Scanner` gives up on a line over 64 KiB and the process could then block on a full
//! pipe), lines are cut at 64 KiB, and after the process is reaped the stderr reader is given 2 s
//! to finish (a grandchild that inherited the pipe would hold Go's `<-errDone` for ever).

use crate::util::lock;
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::process::Command;
use tokio::sync::mpsc;

use super::ffmpeg::{self, downlink_args, gain_pcm, scrub_line};
use crate::cancel::CancelToken;
pub use crate::driver::TalkHealth;
use crate::pacer::{Config as PacerConfig, Kind, Pacer};

/// 40 ms of 16 kHz L16: what internal/sfu expects.
pub const DOWN_FRAME_BYTES: usize = 1280;
/// Frames (640 ms) of channel slack between the pacer and the client leg.
pub const DOWN_QUEUE: usize = 16;
pub const DOWN_FRAME_DUR: Duration = Duration::from_millis(40);
const MAX_STDERR_LINE: usize = 64 << 10;
const STDERR_GRACE: Duration = Duration::from_secs(2);

/// `downPacerConfig`: the visitor-audio playout policy (prime about 100 ms, adapt 40 to 200 ms
/// from the measured arrival jitter, backlog capped at 200 ms (oldest dropped), silence on
/// underrun).
pub fn down_pacer_config() -> PacerConfig {
    PacerConfig {
        frame_dur: DOWN_FRAME_DUR,
        initial: Duration::from_millis(100),
        min: Duration::from_millis(40),
        max: Duration::from_millis(200),
        silence: Some(vec![0u8; DOWN_FRAME_BYTES]),
        ..PacerConfig::default()
    }
}

/// `talkStats`: the counters of one call.
#[derive(Default)]
pub struct TalkStats {
    pub up: AtomicBool,
    pub bytes_to_camera: AtomicU64,
    pub frames_from_camera: AtomicU64,
    pub drops_up: AtomicU64,
    pub drops_down: AtomicU64,
    last_ffmpeg: Mutex<String>,
    /// the ffmpeg process ids (0 before the process exists), for the tests that check they are
    /// reaped
    pub up_pid: std::sync::atomic::AtomicU32,
    pub down_pid: std::sync::atomic::AtomicU32,
    down_pacer: Mutex<Option<Arc<Pacer>>>,
    up_pacer: Mutex<Option<Arc<Pacer>>>,
}

impl TalkStats {
    pub fn new() -> Arc<TalkStats> {
        Arc::new(TalkStats::default())
    }

    pub fn set_line(&self, l: String) {
        *lock(&self.last_ffmpeg) = l;
    }

    pub fn last_line(&self) -> String {
        lock(&self.last_ffmpeg).clone()
    }

    pub fn set_down_pacer(&self, p: Arc<Pacer>) {
        *lock(&self.down_pacer) = Some(p);
    }

    pub fn set_up_pacer(&self, p: Arc<Pacer>) {
        *lock(&self.up_pacer) = Some(p);
    }

    pub fn down_pacer(&self) -> Option<Arc<Pacer>> {
        lock(&self.down_pacer).clone()
    }

    pub fn up_pacer(&self) -> Option<Arc<Pacer>> {
        lock(&self.up_pacer).clone()
    }

    /// `snapshot`.
    pub fn snapshot(&self) -> TalkHealth {
        TalkHealth {
            session_up: self.up.load(Ordering::SeqCst),
            bytes_to_camera: self.bytes_to_camera.load(Ordering::SeqCst),
            frames_from_camera: self.frames_from_camera.load(Ordering::SeqCst),
            drops_up: self.drops_up.load(Ordering::SeqCst),
            drops_down: self.drops_down.load(Ordering::SeqCst),
            last_ffmpeg: self.last_line(),
            last_end: String::new(),
        }
    }

    /// `pacerLine`: the one-line per-call audio summary (both directions) logged when the media
    /// closes. The call id comes from the hub: control characters in it are shown as `?`.
    pub fn pacer_line(&self, call_id: &str) -> String {
        let id: String = call_id
            .chars()
            .map(|c| if c.is_control() { '?' } else { c })
            .collect();
        let mut line = format!("portico-transport: talk call={id} audio stats");
        match self.down_pacer() {
            Some(p) => line.push_str(&format!(
                " down[{} fullQueueDrops={}]",
                p.stats(),
                self.drops_down.load(Ordering::SeqCst)
            )),
            None => line.push_str(" down[none]"),
        }
        match self.up_pacer() {
            Some(p) => line.push_str(&format!(
                " up[{} fullQueueDrops={}]",
                p.stats(),
                self.drops_up.load(Ordering::SeqCst)
            )),
            None => line.push_str(" up[none]"),
        }
        line
    }
}

/// `downlink`: the control of one ffmpeg pull. Created before the pull starts, so its owner can
/// be wired up first (`start_downlink` calls `on_exit`, which may need it).
#[derive(Clone, Default)]
pub struct Downlink {
    stop: CancelToken,
    done: CancelToken,
}

impl Downlink {
    pub fn new() -> Downlink {
        Downlink::default()
    }

    /// Asks the owning task to kill ffmpeg.
    pub fn close_nowait(&self) {
        self.stop.cancel();
    }

    /// `Close`: kills ffmpeg and waits for the reader to finish (and close out). A test seam.
    #[cfg(feature = "test-seams")]
    pub async fn close(&self) {
        self.stop.cancel();
        self.done.cancelled().await;
    }

    /// Set when the pull is over and `out` is closed (also when it never started).
    pub fn done(&self) -> CancelToken {
        self.done.clone()
    }
}

/// `startDownlink`: launches the pull. The reader owns `out` and closes it when the pull is
/// over, then calls `on_exit` so the media can end the call. Cancelling `ctx` kills the process
/// (`exec.CommandContext`). `ffmpeg_bin` is the binary as the open looked it up (once). Needs a
/// tokio runtime.
#[allow(clippy::too_many_arguments)]
pub fn start_downlink<F>(
    l: &Downlink,
    ctx: &CancelToken,
    ffmpeg_bin: &std::path::Path,
    rtsp_url: &str,
    gain: i64,
    out: mpsc::Sender<Vec<u8>>,
    attached: Arc<AtomicBool>,
    st: Arc<TalkStats>,
    secret: &str,
    on_exit: F,
) -> Result<(), String>
where
    F: FnOnce() + Send + 'static,
{
    let fail = |m: String| {
        l.done.cancel(); // never started: nothing to wait for
        m
    };
    #[cfg(feature = "test-seams")]
    let injected = ffmpeg::take_failure(ffmpeg::FAIL_DOWNLINK_SPAWN);
    #[cfg(not(feature = "test-seams"))]
    let injected = false;
    let spawned = if injected {
        Err(std::io::Error::new(
            std::io::ErrorKind::OutOfMemory,
            "injected by a test",
        ))
    } else {
        Command::new(ffmpeg_bin)
            .args(downlink_args(rtsp_url))
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
    };
    let mut child = spawned.map_err(|e| {
        fail(format!(
            "starting the downlink ffmpeg: {}",
            crate::gojson::io_err(&e)
        ))
    })?;
    let (Some(stdout), Some(stderr)) = (child.stdout.take(), child.stderr.take()) else {
        let _ = child.start_kill();
        return Err(fail("starting the downlink ffmpeg: no pipes".to_string()));
    };

    // The pacer turns the reader's bursts into one frame per 40 ms. It starts with the pull, but
    // emits nothing until the first real frame has been primed, so the first audio to the phone
    // is as before. `out` lives in a slot so the reader can close it at a known moment (after
    // the pacer is stopped: `stop` returns only when no emit is running).
    let slot = Arc::new(Mutex::new(Some(out)));
    let pc = Arc::new(Pacer::new(down_pacer_config()));
    st.set_down_pacer(pc.clone());
    {
        let (slot, st) = (slot.clone(), st.clone());
        pc.start(move |fr, _k: Kind| {
            let g = lock(&slot);
            if let Some(tx) = g.as_ref() {
                if let Err(mpsc::error::TrySendError::Full(_)) = tx.try_send(fr) {
                    st.drops_down.fetch_add(1, Ordering::SeqCst);
                }
            }
        });
    }

    st.down_pid.store(child.id().unwrap_or(0), Ordering::SeqCst);
    let (stop, done, ctx) = (l.stop.clone(), l.done.clone(), ctx.clone());
    ffmpeg::spawn_guarded(
        "unifi_protect_talk downlink",
        secret.to_string(),
        async move {
            // however this task ends (a panic included): the pacer is stopped, `out` is closed, `done`
            // is set and `on_exit` runs, so the media ends the call instead of waiting for ever
            let mut cleanup = Cleanup {
                pc: pc.clone(),
                slot,
                done,
                on_exit: Some(on_exit),
            };
            let err_task = tokio::spawn(read_stderr(stderr, st.clone()));
            let mut stdout = stdout;
            let mut buf = vec![0u8; DOWN_FRAME_BYTES];
            loop {
                tokio::select! {
                    biased;
                    _ = stop.cancelled() => break,
                    _ = ctx.cancelled() => break,
                    r = stdout.read_exact(&mut buf) => {
                        if r.is_err() {
                            break;
                        }
                        ffmpeg::maybe_panic(ffmpeg::PANIC_DOWNLINK);
                        st.frames_from_camera.fetch_add(1, Ordering::SeqCst);
                        if !attached.load(Ordering::SeqCst) {
                            continue;
                        }
                        let fr = gain_pcm(&buf, gain).into_owned();
                        pc.push(pc.now(), fr);
                    }
                }
            }
            // the process is killed (a no-op when it is already gone), then reaped
            let _ = child.kill().await;
            reap_stderr(err_task).await;
            cleanup.run();
        },
    );
    Ok(())
}

/// What the downlink task always does at its end: the pacer is stopped (before the close of `out`:
/// `stop` returns only when no emit is running), `out` is closed, `done` is set, `on_exit` runs.
struct Cleanup<F: FnOnce()> {
    pc: Arc<Pacer>,
    slot: Arc<Mutex<Option<mpsc::Sender<Vec<u8>>>>>,
    done: CancelToken,
    on_exit: Option<F>,
}

impl<F: FnOnce()> Cleanup<F> {
    fn run(&mut self) {
        self.pc.stop();
        drop(lock(&self.slot).take());
        self.done.cancel();
        if let Some(f) = self.on_exit.take() {
            f();
        }
    }
}

impl<F: FnOnce()> Drop for Cleanup<F> {
    fn drop(&mut self) {
        self.run();
    }
}

async fn reap_stderr(t: tokio::task::JoinHandle<()>) {
    let abort = t.abort_handle();
    if tokio::time::timeout(STDERR_GRACE, t).await.is_err() {
        abort.abort();
    }
}

/// The last stderr line, URL-free. Always read to the end so the process never blocks on a full
/// pipe; a line is cut at 64 KiB.
async fn read_stderr<R: AsyncRead + Unpin>(mut r: R, st: Arc<TalkStats>) {
    let mut chunk = [0u8; 4096];
    let mut line: Vec<u8> = Vec::new();
    loop {
        let n = match r.read(&mut chunk).await {
            Ok(0) | Err(_) => break,
            Ok(n) => n,
        };
        for &b in &chunk[..n] {
            if b == b'\n' {
                st.set_line(scrub_line(&String::from_utf8_lossy(&line)));
                line.clear();
            } else if line.len() < MAX_STDERR_LINE {
                line.push(b);
            }
        }
    }
    if !line.is_empty() {
        st.set_line(scrub_line(&String::from_utf8_lossy(&line)));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn health_wire_form_is_gos() {
        let h = TalkHealth {
            session_up: true,
            bytes_to_camera: 640,
            frames_from_camera: 3,
            drops_up: 1,
            drops_down: 2,
            last_ffmpeg: "a<b>&\"c\u{1}".to_string(),
            last_end: "hung up".to_string(),
        };
        assert_eq!(
            h.to_wire(),
            "{\"session_up\":true,\"bytes_to_camera\":640,\"frames_from_camera\":3,\"drops_up\":1,\"drops_down\":2,\"last_ffmpeg\":\"a\\u003cb\\u003e\\u0026\\\"c\\u0001\",\"last_end\":\"hung up\"}"
        );
        assert_eq!(
            TalkHealth::default().to_wire(),
            "{\"session_up\":false,\"bytes_to_camera\":0,\"frames_from_camera\":0,\"drops_up\":0,\"drops_down\":0,\"last_ffmpeg\":\"\",\"last_end\":\"\"}"
        );
    }

    #[test]
    fn the_pacer_line_names_both_directions_and_hides_control_characters() {
        let st = TalkStats::new();
        assert_eq!(
            st.pacer_line("c1"),
            "portico-transport: talk call=c1 audio stats down[none] up[none]"
        );
        assert!(st.pacer_line("a\nb\x1b[31m").contains("call=a?b?[31m "));
    }

    #[tokio::test]
    async fn stderr_lines_are_scrubbed_capped_and_always_drained() {
        let st = TalkStats::new();
        let (mut w, r) = tokio::io::duplex(64);
        let t = tokio::spawn(read_stderr(r, st.clone()));
        use tokio::io::AsyncWriteExt;
        w.write_all(b"first rtsp://u:p@h/x line\n").await.unwrap();
        // a line far over the cap: still drained to its end, kept to 300 bytes by the scrub
        let big = vec![b'z'; 200_000];
        w.write_all(&big).await.unwrap();
        w.write_all(b"\nlast one http://t/o\nunterminated")
            .await
            .unwrap();
        drop(w);
        t.await.unwrap();
        assert_eq!(st.last_line(), "unterminated");
    }
}
