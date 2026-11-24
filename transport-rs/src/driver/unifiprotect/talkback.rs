//! `talkback.go`: one open talkback session of the `unifi_protect` driver. Client PCM (16 kHz
//! L16 mono, frames of any even length) goes in; an `ffmpeg` subprocess (the "ffmpeg alongside
//! the binary" installer prerequisite, §9.9) transcodes it to the codec the REST-opened session
//! expects and pushes RTP at the camera. Send-only: nothing comes back (the visitor's voice rides
//! the RTSP feed, §9.6).
//!
//! [`FfmpegOpener`] is the [`TalkbackOpener`] the driver installs by default (the seam of `mod.rs`),
//! so `open_relay` and the answer of a ring open the real session.
//!
//! Resource rules (PARITY.md 129 to 132): the process is spawned with `kill_on_drop`, one task
//! owns it and always reaps it (no zombie, however the session ends: a close, the driver's
//! context, the process dying, a panic), `done` is set by a drop guard, the input queue holds 64
//! frames (a full queue drops the NEW frame at the sender), a write that does not complete in 5 s
//! ends the session (Go would block for ever on a stalled `ffmpeg`), and `ffmpeg`'s stderr is
//! discarded as in Go.

use crate::util::lock;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::AsyncWriteExt;
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::mpsc;

use super::api::TalkbackSession;
use super::ffmpeg::{self, exit_text, ffmpeg_args, look_path_blocking, valid_sink};
use super::{OpenedTalkback, TalkbackOpener};
use crate::cancel::CancelToken;
use crate::driver::{BoxFuture, DriverError, TalkbackSink};

/// Frames buffered ahead of ffmpeg's stdin; a full queue drops the new frame.
pub const FRAME_QUEUE: usize = 64;
/// A write to ffmpeg's stdin that takes longer than this ends the session.
pub const WRITE_TIMEOUT: Duration = Duration::from_secs(5);
/// How long a failed write waits for the process to explain itself (Go: one second).
const EXIT_GRACE: Duration = Duration::from_secs(1);

/// `Talkback`.
pub struct Talkback {
    pid: Option<u32>,
    tx: mpsc::Sender<Vec<u8>>,
    stop: CancelToken,
    done: CancelToken,
    err: Arc<Mutex<Option<String>>>,
}

impl Talkback {
    /// The process id of ffmpeg (tests check it is reaped).
    pub fn pid(&self) -> Option<u32> {
        self.pid
    }

    /// `In()`: where 16 kHz L16 mono frames of any even length go. Never closed by the
    /// Talkback; `close` ends the session.
    pub fn input(&self) -> mpsc::Sender<Vec<u8>> {
        self.tx.clone()
    }

    /// `Done()`: set when the session is over (`close`, or ffmpeg exited).
    pub fn done(&self) -> CancelToken {
        self.done.clone()
    }

    /// `Err()`: why the session ended on its own, `None` after a plain close. A test seam.
    #[cfg(feature = "test-seams")]
    pub fn err(&self) -> Option<String> {
        lock(&self.err).clone()
    }

    /// `Close`, without the wait: asks the owning task to kill ffmpeg. Idempotent.
    pub fn close_nowait(&self) {
        self.stop.cancel();
    }

    /// `Close`: stops ffmpeg and waits until the process is reaped.
    pub async fn close(&self) {
        self.stop.cancel();
        self.done.cancelled().await;
    }
}

impl TalkbackSink for Talkback {
    fn input(&self) -> mpsc::Sender<Vec<u8>> {
        self.tx.clone()
    }
    fn close(&self) -> Result<(), DriverError> {
        self.stop.cancel();
        Ok(())
    }
    fn closed(&self) -> BoxFuture<'_, ()> {
        Box::pin(async move { self.done.cancelled().await })
    }
}

/// Sets `done` however the owning task ends (a panic included).
struct DoneGuard(CancelToken);

impl Drop for DoneGuard {
    fn drop(&mut self) {
        self.0.cancel();
    }
}

/// `startTalkback`: launches ffmpeg (`ffmpeg` is the binary name or path) for the session and
/// the task that feeds it. Cancelling `ctx` stops it. Needs a tokio runtime. A test seam (R2-153):
/// it looks the binary up synchronously, which production never does.
#[cfg(feature = "test-seams")]
pub fn start_talkback(
    ctx: &CancelToken,
    ffmpeg: &str,
    s: &TalkbackSession,
) -> Result<Talkback, String> {
    start_talkback_with(ctx, ffmpeg, s, WRITE_TIMEOUT, "")
}

/// [`start_talkback`] with the write timeout a test shortens and the secret (the API key) to
/// scrub from the text of a panic of the owning task.
#[cfg(feature = "test-seams")]
pub fn start_talkback_with(
    ctx: &CancelToken,
    ffmpeg: &str,
    s: &TalkbackSession,
    write_timeout: Duration,
    secret: &str,
) -> Result<Talkback, String> {
    let args = prepare(s)?;
    let path = super::ffmpeg::look_path(ffmpeg)
        .map_err(|e| format!("ffmpeg not found (installer prerequisite on HA Core, §9.9): {e}"))?;
    spawn_uplink(ctx, &path, &args, write_timeout, secret)
}

/// [`start_talkback_with`] for a binary that was already looked up (once per open, off the
/// runtime: see [`look_path_blocking`]).
pub fn start_talkback_at(
    ctx: &CancelToken,
    ffmpeg: &Path,
    s: &TalkbackSession,
    write_timeout: Duration,
    secret: &str,
) -> Result<Talkback, String> {
    let args = prepare(s)?;
    spawn_uplink(ctx, ffmpeg, &args, write_timeout, secret)
}

/// The checks of a session and the argument list of its transcode.
fn prepare(s: &TalkbackSession) -> Result<Vec<String>, String> {
    valid_sink(&s.url)?;
    let rate = if s.sampling_rate <= 0 {
        24000
    } else {
        s.sampling_rate
    };
    ffmpeg_args(&s.url, &s.codec, rate)
}

fn spawn_uplink(
    ctx: &CancelToken,
    path: &Path,
    args: &[String],
    write_timeout: Duration,
    secret: &str,
) -> Result<Talkback, String> {
    let mut child = Command::new(path)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("starting ffmpeg: {}", crate::gojson::io_err(&e)))?;
    let Some(stdin) = child.stdin.take() else {
        let _ = child.start_kill();
        return Err("starting ffmpeg: no stdin".to_string());
    };
    let (tx, rx) = mpsc::channel(FRAME_QUEUE);
    let t = Talkback {
        pid: child.id(),
        tx,
        stop: CancelToken::new(),
        done: CancelToken::new(),
        err: Arc::new(Mutex::new(None)),
    };
    let (ctx, stop, done, err) = (ctx.clone(), t.stop.clone(), t.done.clone(), t.err.clone());
    ffmpeg::spawn_guarded("unifi_protect talkback", secret.to_string(), async move {
        let _done = DoneGuard(done);
        run_uplink(child, stdin, rx, ctx, stop, err, write_timeout).await;
    });
    Ok(t)
}

fn set_err(err: &Mutex<Option<String>>, text: String) {
    let mut g = lock(err);
    if g.is_none() {
        *g = Some(text);
    }
}

async fn kill_and_reap(child: &mut Child) {
    let _ = child.kill().await;
}

/// The owning task: the pump of Go's `startTalkback` plus its `cmd.Wait` goroutine.
async fn run_uplink(
    mut child: Child,
    mut stdin: ChildStdin,
    mut rx: mpsc::Receiver<Vec<u8>>,
    ctx: CancelToken,
    stop: CancelToken,
    err: Arc<Mutex<Option<String>>>,
    write_timeout: Duration,
) {
    let exited = |err: &Mutex<Option<String>>, st: std::io::Result<std::process::ExitStatus>| {
        if let Ok(st) = st {
            if let Some(t) = exit_text(&st) {
                set_err(err, format!("ffmpeg exited: {t}"));
            }
        }
    };
    loop {
        tokio::select! {
            biased;
            _ = ctx.cancelled() => { kill_and_reap(&mut child).await; return; }
            _ = stop.cancelled() => { kill_and_reap(&mut child).await; return; }
            st = child.wait() => { exited(&err, st); return; }
            f = rx.recv() => {
                // every sender gone: nobody can feed or close this session any more
                let Some(frame) = f else { kill_and_reap(&mut child).await; return };
                if frame.is_empty() {
                    continue;
                }
                ffmpeg::maybe_panic(ffmpeg::PANIC_UPLINK);
                let write = tokio::time::timeout(write_timeout, stdin.write_all(&frame));
                tokio::select! {
                    biased;
                    _ = ctx.cancelled() => { kill_and_reap(&mut child).await; return; }
                    _ = stop.cancelled() => { kill_and_reap(&mut child).await; return; }
                    st = child.wait() => { exited(&err, st); return; }
                    r = write => match r {
                        Ok(Ok(())) => {}
                        Ok(Err(_)) => {
                            // ffmpeg went away; its exit status explains it
                            tokio::select! {
                                st = child.wait() => {
                                    let t = match st {
                                        Ok(st) => exit_text(&st).unwrap_or_else(|| "<nil>".to_string()),
                                        Err(_) => "<nil>".to_string(),
                                    };
                                    set_err(&err, format!("ffmpeg exited: {t}"));
                                    return;
                                }
                                _ = tokio::time::sleep(EXIT_GRACE) => {}
                            }
                        }
                        Err(_) => {
                            set_err(&err, "ffmpeg stopped reading its input".to_string());
                            kill_and_reap(&mut child).await;
                            return;
                        }
                    }
                }
            }
        }
    }
}

/// The [`TalkbackOpener`] the driver installs: the real `ffmpeg` talkback.
pub struct FfmpegOpener {
    ffmpeg: String,
    /// the API key, scrubbed from the text of a panic
    key: String,
    /// where the preflight found the binary: `start` uses that one (the binary is looked up
    /// once per open, off the runtime, and the one that was checked is the one that runs)
    found: Mutex<Option<PathBuf>>,
}

impl FfmpegOpener {
    /// `ffmpeg` is the binary name (looked up on `PATH` when the session opens) or a path.
    pub fn new(ffmpeg: &str, key: &str) -> Self {
        FfmpegOpener {
            ffmpeg: ffmpeg.to_string(),
            key: key.to_string(),
            found: Mutex::new(None),
        }
    }
}

impl TalkbackOpener for FfmpegOpener {
    fn preflight(&self) -> BoxFuture<'_, Result<(), DriverError>> {
        Box::pin(async move {
            match look_path_blocking(&self.ffmpeg).await {
                Ok(p) => {
                    *lock(&self.found) = Some(p);
                    Ok(())
                }
                Err(e) => Err(DriverError::new(format!(
                    "ffmpeg not found (installer prerequisite on HA Core, §9.9): {e}"
                ))),
            }
        })
    }

    fn start<'a>(
        &'a self,
        ctx: &'a CancelToken,
        session: TalkbackSession,
    ) -> BoxFuture<'a, Result<OpenedTalkback, DriverError>> {
        Box::pin(async move {
            let found = lock(&self.found).take();
            let path = match found {
                Some(p) => p,
                // `start` without a preflight (a test)
                None => look_path_blocking(&self.ffmpeg).await.map_err(|e| {
                    DriverError::new(format!(
                        "ffmpeg not found (installer prerequisite on HA Core, §9.9): {e}"
                    ))
                })?,
            };
            // the session holds the RTP URL: it is passed on, never formatted
            let tb = start_talkback_at(ctx, &path, &session, WRITE_TIMEOUT, &self.key)
                .map_err(DriverError::new)?;
            let done = tb.done();
            Ok(OpenedTalkback {
                sink: Arc::new(tb),
                done,
            })
        })
    }
}
