//! `talk_media.go`: the media of one `unifi_protect_talk` call (front-door talk plan T4): the
//! single-session guard, the console's talkback-session, and the uplink (phone microphone ->
//! ffmpeg libopus -> RTP to the camera's sink). The downlink (the visitor's voice) is
//! `talk_down.rs` (T5).
//!
//! Order, all of it inside `answer_with_audio` and so only after a client leg has connected: (1)
//! guard (when a `guard_url` is set: fail closed; empty: no external guard); (1b) the cheap local checks (a downlink URL, an ffmpeg binary), so no
//! session is armed for a call that cannot run; (2) POST talkback-session; (3) start ffmpeg on
//! the sink the console returned and the downlink pull. Anything that fails leaves the ring
//! answerable.
//!
//! Closing: there is NO published endpoint to tear a talkback session down (bell-talk's
//! protect.go documents the same, and does not invent one against the live door). Closing the
//! media stops ffmpeg, which stops the RTP; the console's session lapses on its own (how fast is
//! UNVERIFIED, and is what the call-button check U6 of the plan is about). A session opened but
//! never used (ffmpeg fails to start) is armed until then; that window is the cost of the order
//! above and is why the guard and every cheap check come first.
//!
//! Differences from Go (PARITY.md 137 to 142): `close` asks and the teardown runs on a task (the
//! trait is called under the driver's synchronous locks), `closed` is the wait; the guard needs
//! plain `http://` (an `https://` guard URL is refused, fail closed, with Go's "unreachable"
//! text); a body that is not valid UTF-8 is "not JSON" here (Go repairs it) which only ever
//! refuses; the uplink pacer's silence frame is capped at 16 KiB.

use crate::util::lock;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::time::Duration;

use tokio::sync::{mpsc, Mutex as AsyncMutex};
use tokio::task::JoinHandle;

use super::api::{decode_talkback_session, path_escape, ProtectApi, TalkbackSession, API_BASE};
use super::ffmpeg::{self, look_path_blocking, scrub_line};
use super::talk::{TalkEnv, TalkMedia, TalkOpener};
use super::talk_down::{start_downlink, Downlink, TalkStats, DOWN_QUEUE};
use super::talkback::{start_talkback_at, Talkback, WRITE_TIMEOUT};
use crate::cancel::CancelToken;
use crate::driver::{BoxFuture, CallAudio, DriverError, Sentinel};
use crate::gostd::http1::{FetchError, HttpClient};
use crate::gostd::url as gourl;
use crate::pacer::{Config as PacerConfig, Kind, Pacer};

/// 16 kHz L16 mono is 32 bytes per millisecond.
pub const UP_BYTES_PER_MS: usize = 32;
const MAX_GUARD_BODY: usize = 4 << 10;
/// The uplink queue (client frames waiting for the pump).
pub const UP_QUEUE: usize = 64;
const MAX_SILENCE: usize = 16 << 10;

fn refuse(what: &str) -> DriverError {
    DriverError::new(format!(
        "unifi_protect_talk: {what}, refusing to open a session"
    ))
}

/// `checkSessionGuard`: asks the other relay (bell-talk's `/health`) whether it holds the
/// camera's one talkback sink. With a URL set, only an explicit `{"sessionUp": false}` lets the
/// answer go on: busy is `Busy`, and anything else (unreachable, timeout, a status other than 200,
/// malformed JSON, a missing or non-boolean field) refuses too, because a quiet guess is how the
/// call button dies.
///
/// An EMPTY `guard_url` means "no external guard" (PARITY.md 476): the old relay is retired or
/// never existed, there is nobody to ask, and the open goes on. The door's own one-call-per-door
/// rule (`Busy` in `talk.rs` / `talk_dial.rs`) still applies, so Portico never opens two sessions
/// itself; that rule, not this check, is what protects the camera once bell-talk is gone.
pub async fn check_session_guard(
    ctx: &CancelToken,
    guard_url: &str,
    timeout: Duration,
) -> Result<(), DriverError> {
    if guard_url.is_empty() {
        return Ok(());
    }
    let (dial, host_header, path, tls_name) = match guard_target(guard_url) {
        Ok(t) => t,
        // `http.NewRequest` refused the URL
        Err(GuardTarget::Unusable) => {
            return Err(DriverError::new(
                "unifi_protect_talk: guard_url is not usable",
            ))
        }
        // `client.Do` refuses it (no host, a scheme it does not speak) or this client cannot
        // (user:password@: PARITY.md 140): both read as an unreachable guard
        Err(GuardTarget::Unreachable) => return Err(refuse("the session guard is unreachable")),
    };
    let mut client = HttpClient::new(dial, host_header, path);
    if let Some(name) = tls_name {
        client = client.with_tls(std::sync::Arc::new(crate::gostd::tls::Tls::new(true)), name);
    }
    let (status, fetched) = tokio::select! {
        _ = ctx.cancelled() => (0, Err(FetchError::Url("context canceled".to_string()))),
        r = client.get_status(timeout, MAX_GUARD_BODY) => r,
    };
    // Go checks the status before it reads the body (talk_media.go:75-81): a non-200 answer whose
    // body is cut off or late is still "answered HTTP N" (R2-74)
    let wrong_status = |status: u16| {
        DriverError::new(format!(
            "unifi_protect_talk: the session guard answered HTTP {status}, refusing to open a session"
        ))
    };
    let f = match fetched {
        Ok(f) => f,
        Err(FetchError::Url(_)) => {
            return Err(refuse("the session guard is unreachable"));
        }
        Err(FetchError::Body(_)) if status != 0 && status != 200 => {
            return Err(wrong_status(status));
        }
        Err(FetchError::Body(_)) => {
            return Err(refuse("the session guard reply was cut off"));
        }
    };
    if f.status != 200 {
        return Err(wrong_status(f.status));
    }
    let Some(entries) = parse_object_or_null(&f.body) else {
        return Err(refuse("the session guard reply is not JSON"));
    };
    // map[string]json.RawMessage: the last duplicate wins
    let value = entries
        .iter()
        .rev()
        .find(|(k, _)| k == "sessionUp")
        .map(|(_, v)| v.get().to_string())
        .unwrap_or_default();
    match value.as_str() {
        "false" => Ok(()),
        "true" => Err(DriverError::wrapping(
            Sentinel::Busy,
            "unifi_protect_talk: the old intercom holds the talkback session: busy",
        )),
        _ => Err(refuse("the session guard reply has no boolean sessionUp")),
    }
}

/// A JSON object (or `null`, an empty one) as `json.Unmarshal` into a map reads it.
fn parse_object_or_null(body: &[u8]) -> Option<Vec<(String, Box<serde_json::value::RawValue>)>> {
    // JSON white space is four characters, not Unicode's (R2-8: `json.Unmarshal` refuses the rest)
    let text = crate::gostd::text::trim_json_space(std::str::from_utf8(body).ok()?);
    if text == "null" {
        return Some(Vec::new());
    }
    crate::ipc::parse_object_entries(text.as_bytes())
}

/// Why a guard URL cannot be fetched.
#[derive(Debug, PartialEq, Eq)]
enum GuardTarget {
    /// `http.NewRequest` fails on it
    Unusable,
    /// the request is refused or cannot be made here
    Unreachable,
}

/// Where a guard URL goes: the dial address, the `Host` header, the request target, and for an
/// `https://` URL the name the certificate must carry (R2-117: the client speaks TLS, verified
/// against the system roots, as Go's default client does).
type GuardAddr = (String, String, String, Option<String>);

fn guard_target(raw: &str) -> Result<GuardAddr, GuardTarget> {
    let u = gourl::parse(raw).map_err(|_| GuardTarget::Unusable)?;
    // Go's client refuses a URL with no host or a scheme it does not speak; this one sends no
    // credentials
    let https = u.scheme == "https";
    if !(u.scheme == "http" || https) || u.has_user || u.host.is_empty() || !u.opaque.is_empty() {
        return Err(GuardTarget::Unreachable);
    }
    // as net/http: dial `Hostname():Port()` with the scheme's port for an absent or empty port,
    // send the `Host` header without an empty port (R2-7)
    Ok((
        u.dial_addr(if https { "443" } else { "80" }),
        u.host_header(),
        u.request_uri(),
        https.then(|| u.request_host_port().0),
    ))
}

/// `openTalkSession`: POST talkback-session as bell-talk sends it (an empty JSON object) with
/// its checks: the sink URL and the sampling rate must be present, because a plausible guessed
/// rate plays speech at the wrong speed.
pub async fn open_talk_session(
    api: &ProtectApi,
    ctx: &CancelToken,
    camera_id: &str,
) -> Result<TalkbackSession, String> {
    let data = api
        .request(
            ctx,
            "POST",
            &format!("{API_BASE}{}/talkback-session", path_escape(camera_id)),
            Some(b"{}"),
        )
        .await?;
    let mut s = decode_talkback_session(&data)?;
    s.codec = ffmpeg::go_trim_space(&s.codec).to_lowercase();
    if s.url.is_empty() {
        return Err("talkback-session: no url in the response".to_string());
    }
    if s.sampling_rate <= 0 {
        return Err("talkback-session: no samplingRate in the response".to_string());
    }
    Ok(s)
}

/// `talkSessionMedia`: the opened media as a [`TalkMedia`].
pub struct SessionMedia {
    me: Weak<SessionMedia>,
    call_id: String,
    /// the API key, scrubbed from the text of a panic
    key: String,
    tb: Arc<Talkback>,
    dl: Downlink,
    st: Arc<TalkStats>,
    /// client frames; the pump hands them to ffmpeg, counting what it cannot
    in_tx: mpsc::Sender<Vec<u8>>,
    out_rx: Arc<AsyncMutex<mpsc::Receiver<Vec<u8>>>>,
    /// set when both ffmpegs are gone (`m.done`): the driver ends the call on it
    done: CancelToken,
    /// set when the pump returned and the last line was logged
    finished: CancelToken,
    pumped: CancelToken,
    attached: Arc<AtomicBool>,
    orphan: Mutex<Option<JoinHandle<()>>>,
    /// `Close` has begun: what stops now was stopped on purpose, not a failure
    closing: AtomicBool,
    closer: AtomicBool,
    /// why the media ended on its own (first writer wins); `None` for a hangup
    reason: Mutex<Option<String>>,
}

impl SessionMedia {
    fn set_reason(&self, r: &str) {
        if self.closing.load(Ordering::SeqCst) {
            return;
        }
        let mut g = lock(&self.reason);
        if g.is_none() {
            *g = Some(scrub_line(r));
        }
    }

    fn line_or(&self, what: &str) -> String {
        let l = self.st.last_line();
        if l.is_empty() {
            what.to_string()
        } else {
            format!("{what}: {l}")
        }
    }

    /// `end`: the orphan cap is off, the session is down, `done` is set.
    fn end(&self) {
        if let Some(h) = lock(&self.orphan).take() {
            h.abort();
        }
        self.st.up.store(false, Ordering::SeqCst);
        self.done.cancel();
    }

    fn arm_orphan(&self, wait: Duration) {
        let me = self.me.clone();
        let h = tokio::spawn(async move {
            tokio::time::sleep(wait).await;
            if let Some(m) = me.upgrade() {
                if !m.attached.load(Ordering::SeqCst) {
                    m.set_reason("no client attached within the grace period");
                    m.close();
                }
            }
        });
        if let Some(old) = lock(&self.orphan).replace(h) {
            old.abort();
        }
    }
}

impl CallAudio for SessionMedia {
    /// The client leg's 16 kHz L16 mono frames, of any even length.
    fn input(&self) -> mpsc::Sender<Vec<u8>> {
        self.in_tx.clone()
    }
    fn output(&self) -> Arc<AsyncMutex<mpsc::Receiver<Vec<u8>>>> {
        self.out_rx.clone()
    }
    /// A client leg has taken the pair: the orphan cap no longer applies.
    fn attach(&self) {
        self.attached.store(true, Ordering::SeqCst);
        if let Some(h) = lock(&self.orphan).take() {
            h.abort();
        }
    }
    /// A no-op here: the driver's wrapper hangs the call up, which closes.
    fn detach(&self) {}
}

impl TalkMedia for SessionMedia {
    /// Stops both ffmpegs (the RTP to the camera ends with the uplink); the teardown task waits
    /// for them.
    fn close(&self) {
        if self.closer.swap(true, Ordering::SeqCst) {
            return;
        }
        self.closing.store(true, Ordering::SeqCst);
        self.tb.close_nowait();
        self.dl.close_nowait();
        let Some(m) = self.me.upgrade() else { return };
        ffmpeg::spawn_guarded(
            "unifi_protect_talk teardown",
            self.key.clone(),
            async move {
                // however this ends (a panic included) the media is over and `closed` resolves
                struct Fin(Arc<SessionMedia>);
                impl Drop for Fin {
                    fn drop(&mut self) {
                        self.0.end();
                        self.0.finished.cancel();
                    }
                }
                let fin = Fin(m);
                let m = &fin.0;
                m.tb.done().cancelled().await;
                m.dl.done().cancelled().await;
                ffmpeg::maybe_panic(ffmpeg::PANIC_TEARDOWN);
                m.end();
                m.pumped.cancelled().await;
                crate::panic_hook::out_line(&m.st.pacer_line(&m.call_id));
            },
        );
    }

    fn closed(&self) -> BoxFuture<'_, ()> {
        Box::pin(async move { self.finished.cancelled().await })
    }

    fn done(&self) -> CancelToken {
        self.done.clone()
    }

    fn end_reason(&self) -> Option<String> {
        lock(&self.reason).clone()
    }

    /// Re-arms the unattached-session cap (an outbound call gives the phone longer to connect).
    fn set_orphan_grace(&self, d: Duration) {
        if !self.attached.load(Ordering::SeqCst) {
            self.arm_orphan(d);
        }
    }
}

/// `upPacerConfig`: the microphone-path policy for frames of the given duration: a short cushion
/// (the phone side already has its own jitter buffer in `sfu`, so this one only evens the
/// cadence into ffmpeg and must add little delay), backlog capped at 160 ms with the oldest
/// dropped.
pub fn up_pacer_config(frame: Duration, bytes: usize) -> PacerConfig {
    PacerConfig {
        frame_dur: frame,
        initial: 2 * frame,
        min: frame,
        max: Duration::from_millis(160),
        silence: Some(vec![0u8; bytes.min(MAX_SILENCE)]),
        ..PacerConfig::default()
    }
}

/// `pump`: hands client frames to the uplink ffmpeg through a pacer, so ffmpeg (and so the RTP to
/// the camera) sees one frame per frame duration whatever the arrival pattern. The frame
/// duration is taken from the first frame (16 kHz L16: 32 bytes per ms). The pacer's output
/// queue (the talkback's input) full drops the new frame and counts it.
async fn pump(m: Arc<SessionMedia>, mut rx: mpsc::Receiver<Vec<u8>>) {
    // `pumped` is set however the pump ends; a pump that ends while the media is still up (a
    // panic) ends the call: a call the phone can speak into but nobody hears must not read as
    // healthy (Go's panic would end the process)
    struct Fin(Arc<SessionMedia>);
    impl Drop for Fin {
        fn drop(&mut self) {
            if !self.0.done.is_cancelled() {
                self.0.set_reason("the microphone pump stopped");
                self.0.close();
            }
            self.0.pumped.cancel();
        }
    }
    let _fin = Fin(m.clone());
    let done = m.done.clone();
    // the pacer is stopped however the pump ends
    struct StopPacer(Option<Arc<Pacer>>);
    impl Drop for StopPacer {
        fn drop(&mut self) {
            if let Some(p) = &self.0 {
                p.stop();
            }
        }
    }
    let mut pc = StopPacer(None);
    loop {
        tokio::select! {
            biased;
            _ = done.cancelled() => break,
            fr = rx.recv() => {
                let Some(fr) = fr else { break };
                ffmpeg::maybe_panic(ffmpeg::PANIC_PUMP);
                let p = match &pc.0 {
                    Some(p) => p.clone(),
                    None => {
                        let mut ms = fr.len() / UP_BYTES_PER_MS;
                        if !(5..=120).contains(&ms) {
                            ms = 20; // an odd first frame: assume the usual 20 ms cadence
                        }
                        let p = Arc::new(Pacer::new(up_pacer_config(
                            Duration::from_millis(ms as u64),
                            fr.len(),
                        )));
                        m.st.set_up_pacer(p.clone());
                        let (tx, st) = (m.tb.input(), m.st.clone());
                        p.start(move |f, k| {
                            let n = f.len() as u64;
                            match tx.try_send(f) {
                                Ok(()) => {
                                    if k == Kind::Audio {
                                        st.bytes_to_camera.fetch_add(n, Ordering::SeqCst);
                                    }
                                }
                                // a full queue drops the new frame and counts it; a queue whose
                                // reader is gone (the uplink ended; Go's channel just stays
                                // open with room) is not a drop of audio anybody could hear
                                Err(mpsc::error::TrySendError::Full(_)) => {
                                    st.drops_up.fetch_add(1, Ordering::SeqCst);
                                }
                                Err(mpsc::error::TrySendError::Closed(_)) => {}
                            }
                        });
                        pc.0 = Some(p.clone());
                        p
                    }
                };
                p.push(p.now(), fr);
            }
        }
    }
}

/// The real [`TalkOpener`] (`openTalkMedia`).
pub struct RealOpener;

impl TalkOpener for RealOpener {
    fn open<'a>(
        &'a self,
        ctx: &'a CancelToken,
        env: TalkEnv,
        call_id: &'a str,
    ) -> BoxFuture<'a, Result<Arc<dyn TalkMedia>, DriverError>> {
        Box::pin(async move { open_talk_media(ctx, env, call_id).await })
    }
}

/// `openTalkMedia`.
pub async fn open_talk_media(
    ctx: &CancelToken,
    env: TalkEnv,
    call_id: &str,
) -> Result<Arc<dyn TalkMedia>, DriverError> {
    let cfg = &env.cfg;
    check_session_guard(ctx, &cfg.guard_url, env.timing.guard_timeout).await?;
    if cfg.downlink_url.is_empty() {
        return Err(refuse("no downlink_url configured"));
    }
    // looked up once for this open (both ffmpegs run the binary found here), off the runtime
    let Ok(ffmpeg_bin) = look_path_blocking(&cfg.ffmpeg).await else {
        return Err(refuse("ffmpeg not found"));
    };
    let api = ProtectApi::new(&cfg.scheme, &cfg.host, &cfg.key, cfg.verify_ssl);
    // The errors of the session, the uplink and the downlink go out as they are, as in Go: the
    // API key is already scrubbed from a transport failure by `ProtectApi`, and nothing else in
    // them is secret (R1-11).
    let session = open_talk_session(&api, ctx, &cfg.camera_id)
        .await
        .map_err(|e| {
            DriverError::new(format!(
                "unifi_protect_talk: opening the talkback session: {e}"
            ))
        })?;
    let run = env.run.clone();
    let tb = start_talkback_at(&run, &ffmpeg_bin, &session, WRITE_TIMEOUT, &cfg.key)
        .map_err(|e| DriverError::new(format!("unifi_protect_talk: {e}")))?;
    let tb = Arc::new(tb);
    let st = TalkStats::new();
    st.up.store(true, Ordering::SeqCst);
    st.up_pid.store(tb.pid().unwrap_or(0), Ordering::SeqCst);
    env.set_stats(st.clone());

    let (in_tx, in_rx) = mpsc::channel(UP_QUEUE);
    let (out_tx, out_rx) = mpsc::channel(DOWN_QUEUE);
    let attached = Arc::new(AtomicBool::new(false));
    let dl = Downlink::new();
    let m = Arc::new_cyclic(|me| SessionMedia {
        me: me.clone(),
        call_id: call_id.to_string(),
        key: cfg.key.clone(),
        tb: tb.clone(),
        dl: dl.clone(),
        st: st.clone(),
        in_tx,
        out_rx: Arc::new(AsyncMutex::new(out_rx)),
        done: CancelToken::new(),
        finished: CancelToken::new(),
        pumped: CancelToken::new(),
        attached: attached.clone(),
        orphan: Mutex::new(None),
        closing: AtomicBool::new(false),
        closer: AtomicBool::new(false),
        reason: Mutex::new(None),
    });
    m.arm_orphan(env.timing.orphan);
    let on_exit = {
        let me = Arc::downgrade(&m);
        move || {
            if let Some(m) = me.upgrade() {
                m.set_reason(&m.line_or("the visitor audio (downlink ffmpeg) stopped"));
                m.close();
            }
        }
    };
    if let Err(e) = start_downlink(
        &dl,
        &run,
        &ffmpeg_bin,
        &cfg.downlink_url,
        cfg.gain,
        out_tx,
        attached,
        st.clone(),
        &cfg.key,
        on_exit,
    ) {
        if let Some(h) = lock(&m.orphan).take() {
            h.abort();
        }
        tb.close().await;
        // no session is up any more (Go leaves `session_up` true here: PARITY.md 352)
        st.up.store(false, Ordering::SeqCst);
        return Err(DriverError::new(format!("unifi_protect_talk: {e}")));
    }
    ffmpeg::spawn_guarded(
        "unifi_protect_talk pump",
        cfg.key.clone(),
        pump(m.clone(), in_rx),
    );
    {
        // ffmpeg ending on its own ends the media
        let me = Arc::downgrade(&m);
        let tbd = tb.done();
        ffmpeg::spawn_guarded(
            "unifi_protect_talk uplink watcher",
            cfg.key.clone(),
            async move {
                tbd.cancelled().await;
                if let Some(m) = me.upgrade() {
                    m.set_reason("the microphone path (uplink ffmpeg) stopped");
                    m.close();
                }
            },
        );
    }
    Ok(m)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn guard_targets() {
        let t = |u: &str| guard_target(u);
        assert_eq!(
            t("http://127.0.0.1:8099/health"),
            Ok((
                "127.0.0.1:8099".into(),
                "127.0.0.1:8099".into(),
                "/health".into(),
                None
            ))
        );
        assert_eq!(
            t("http://bell"),
            Ok(("bell:80".into(), "bell".into(), "/".into(), None))
        );
        assert_eq!(
            t("http://bell?x=1#f"),
            Ok(("bell:80".into(), "bell".into(), "/?x=1".into(), None))
        );
        assert_eq!(
            t("http://[::1]:9/h?q"),
            Ok(("[::1]:9".into(), "[::1]:9".into(), "/h?q".into(), None))
        );
        assert_eq!(t("http://[::1]/h").map(|x| x.0), Ok("[::1]:80".into()));
        assert_eq!(t("http://bell/a b").map(|x| x.2), Ok("/a%20b".into()));
        assert_eq!(t("http://bell/a%20b").map(|x| x.2), Ok("/a%20b".into()));
        assert_eq!(t("HTTP://BELL/h").map(|x| x.2), Ok("/h".into()));
        // R2-7: an empty port is the scheme's, and the Host header drops the colon
        assert_eq!(
            t("http://bell:/health"),
            Ok(("bell:80".into(), "bell".into(), "/health".into(), None))
        );
        // R2-117: https is spoken, verified, under the URL's host name
        assert_eq!(
            t("https://bell/h"),
            Ok((
                "bell:443".into(),
                "bell".into(),
                "/h".into(),
                Some("bell".into())
            ))
        );
        assert_eq!(
            t("https://[::1]:8443/h").map(|x| (x.0, x.3)),
            Ok(("[::1]:8443".into(), Some("::1".into())))
        );
        for u in [
            "ftp://bell/x",
            "http://u:p@bell/h",
            "http:///h",
            "/health",
            "",
            "bell:80",
        ] {
            assert_eq!(t(u), Err(GuardTarget::Unreachable), "{u}");
        }
        for u in ["http://bell:notaport/h", "http://[::1/h", "http://bell/%zz"] {
            assert_eq!(t(u), Err(GuardTarget::Unusable), "{u}");
        }
    }

    #[tokio::test]
    async fn the_up_pacer_config_is_the_microphone_policy() {
        let c = up_pacer_config(Duration::from_millis(20), 640);
        assert_eq!(c.initial, Duration::from_millis(40));
        assert_eq!(c.min, Duration::from_millis(20));
        assert_eq!(c.max, Duration::from_millis(160));
        assert_eq!(c.silence.unwrap().len(), 640);
        // the silence frame is bounded whatever the first frame was
        assert_eq!(
            up_pacer_config(Duration::from_millis(20), 10_000_000)
                .silence
                .unwrap()
                .len(),
            MAX_SILENCE
        );
    }
}
