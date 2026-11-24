//! The `ffmpeg` side of the talk family (`talkback.go`, `talk_down.go`): the argument lists, the
//! sink check, the `exec.LookPath` look-alike, the one-line scrub of an `ffmpeg` message, the
//! gain, and a guard that runs a task so that a panic in it is logged (scrubbed) and never
//! leaves a flag unset. The host needs `ffmpeg` as an installer prerequisite (§9.9); nothing is
//! linked into the binary.
//!
//! Every value that ends up in an argument list is checked for control characters and spaces
//! before it gets there (PARITY.md 127): an `ffmpeg` argument is not a shell word, but a
//! newline in a URL is a smell worth refusing.

#[cfg(feature = "test-seams")]
use crate::util::lock;
use std::path::{Path, PathBuf};

pub use crate::gostd::text::go_trim_space;
use crate::gostd::url as gourl;
pub use crate::process::scrub::{scrub_all, scrub_line};

/// Client-leg audio is 16 kHz little-endian L16 mono (the shape internal/sfu and every other leg
/// speak, §9.6); the camera's sink wants Opus (§9.6).
pub const INPUT_RATE: u32 = 16000;

/// `ffmpegBinary`'s default: looked up on `PATH` at the moment it is needed.
pub const FFMPEG_BINARY: &str = "ffmpeg";

/// True when `s` is safe to hand to a process as one argument that names a URL: no control
/// character (0x00 to 0x1f, 0x7f) and no space.
pub fn arg_safe(s: &str) -> bool {
    !s.bytes().any(|b| b <= 0x20 || b == 0x7f)
}

/// `ffmpegArgs`: the transcode command line. `sink` must already be validated (rtp:// or udp://
/// only).
pub fn ffmpeg_args(sink: &str, codec: &str, rate: i64) -> Result<Vec<String>, String> {
    let enc: Vec<String> = match codec {
        "opus" => [
            "-c:a",
            "libopus",
            "-application",
            "voip",
            "-ar",
            &rate.to_string(),
            "-ac",
            "1",
            "-f",
            "rtp",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect(),
        _ => {
            // the codec text comes from the console: cap what an error carries (PARITY.md 153)
            let shown = if codec.len() > 256 {
                let mut end = 256;
                while !codec.is_char_boundary(end) {
                    end -= 1;
                }
                format!("{}...", &codec[..end])
            } else {
                codec.to_string()
            };
            return Err(format!(
                "talkback codec {} is not supported (only opus)",
                crate::gostd::strconv::go_quote(&shown)
            ));
        }
    };
    let mut args: Vec<String> = ["-hide_banner", "-loglevel", "error"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    for a in ["-f", "s16le", "-ar"] {
        args.push(a.to_string());
    }
    args.push(INPUT_RATE.to_string());
    for a in ["-ac", "1", "-i", "pipe:0"] {
        args.push(a.to_string());
    }
    args.extend(enc);
    args.push(sink.to_string());
    Ok(args)
}

/// `validSink`: accepts only an RTP/UDP sink. The URL comes from the console's response; ffmpeg
/// would happily open file: or http: targets.
pub fn valid_sink(raw: &str) -> Result<(), String> {
    let u = match gourl::parse(raw) {
        Ok(u) if !u.hostname().is_empty() => u,
        _ => return Err("talkback sink is not a usable URL".to_string()),
    };
    if u.scheme != "rtp" && u.scheme != "udp" {
        return Err(format!(
            "talkback sink scheme {} is not allowed",
            crate::gostd::strconv::go_quote(&u.scheme)
        ));
    }
    // Rust-only (PARITY.md 127)
    if !arg_safe(raw) {
        return Err("talkback sink is not a usable URL".to_string());
    }
    // Rust-only (PARITY.md 474, DEV-796 D2): the sink is a camera on the LAN, never this host
    // or a group address. Only these three are refused; any other host is the console's call.
    // the authority as written: userinfo is gone from `u.host`, so a second `@` is counted here
    let written = raw.split_once("://").map_or("", |(_, r)| r);
    let written = written.split(['/', '?', '#']).next().unwrap_or("");
    if written.matches('@').count() > 1 || sink_host_denied(&u.host, &u.hostname()) {
        return Err("talkback sink host is not allowed".to_string());
    }
    Ok(())
}

/// `inet_aton`: one to four dot-separated numbers (decimal, `0x` hex, leading-zero octal); the
/// last fills the remaining bytes. glibc, and so ffmpeg, resolves `127.1` and `0x7f000001`.
fn inet_aton(h: &str) -> Option<std::net::Ipv4Addr> {
    let parts: Vec<&str> = h.split('.').collect();
    if parts.is_empty() || parts.len() > 4 {
        return None;
    }
    let mut nums = Vec::new();
    for p in &parts {
        // leading zeros are skipped (strtoul), however many: they must not overflow the parse
        let n = if let Some(x) = p.strip_prefix("0x").or_else(|| p.strip_prefix("0X")) {
            let x = x.trim_start_matches('0');
            if x.is_empty() {
                0
            } else {
                u64::from_str_radix(x, 16).ok()?
            }
        } else if p.len() > 1 && p.starts_with('0') {
            let x = p.trim_start_matches('0');
            if x.is_empty() {
                0
            } else {
                u64::from_str_radix(x, 8).ok()?
            }
        } else {
            p.parse::<u64>().ok()?
        };
        nums.push(n);
    }
    let last = nums.pop()?;
    let mut v: u64 = 0;
    for n in &nums {
        if *n > 255 {
            return None;
        }
        v = (v << 8) | n;
    }
    let rest = 4 - nums.len() as u32;
    if last >= 1u64 << (8 * rest) {
        return None;
    }
    v = (v << (8 * rest)) | last;
    Some(std::net::Ipv4Addr::from(v as u32))
}

fn v4_denied(a: std::net::Ipv4Addr) -> bool {
    a.is_loopback() || a.is_unspecified() || a.is_multicast()
}

/// True for a sink host that is a loopback, unspecified or multicast address (v4 in any
/// `inet_aton` form, v6 including IPv4-mapped and IPv4-compatible, a zone ignored, a trailing
/// dot ignored) or a name that is loopback by definition (`localhost`, `*.localhost`,
/// `ip6-localhost`, `ip6-loopback`). `authority` is the URL's host part as written, `host` its
/// host with the port split off. An unbracketed IPv6 literal (two or more colons without
/// brackets) is refused as unusable: the port split above cannot tell it from `host:port`.
fn sink_host_denied(authority: &str, host: &str) -> bool {
    if !authority.starts_with('[') && authority.matches(':').count() > 1 {
        return true;
    }
    // ffmpeg's av_url_split cuts at the FIRST `]` and the LAST `@`; Go's parser at the last `]`.
    // Where the two could disagree, refuse: a bracket anywhere needs one bracketed IPv6 host
    // that is the whole host part (a second `@` is refused by the caller).
    if authority.contains('[') || authority.contains(']') {
        if host.contains('[') || host.contains(']') || host.parse::<std::net::Ipv6Addr>().is_err()
        {
            // a zone is cut before the check
            let z = host.split('%').next().unwrap_or(host);
            if z.contains('[') || z.contains(']') || z.parse::<std::net::Ipv6Addr>().is_err() {
                return true;
            }
        }
    }
    let h = host.split('%').next().unwrap_or(host);
    let h = h.trim_end_matches('.').to_ascii_lowercase();
    if h == "localhost"
        || h.ends_with(".localhost")
        || h == "ip6-localhost"
        || h == "ip6-loopback"
    {
        return true;
    }
    if let Ok(ip) = h.parse::<std::net::IpAddr>() {
        return match ip.to_canonical() {
            std::net::IpAddr::V4(a) => v4_denied(a),
            std::net::IpAddr::V6(a) => {
                a.is_loopback()
                    || a.is_unspecified()
                    || a.is_multicast()
                    || a.to_ipv4().is_some_and(v4_denied)
            }
        };
    }
    inet_aton(&h).is_some_and(v4_denied)
}

/// `downlinkArgs`: the pull command line. `rtsp_url` must already be validated.
pub fn downlink_args(rtsp_url: &str) -> Vec<String> {
    [
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "s16le",
        "pipe:1",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect()
}

/// `gainPCM`: PCM16 LE `b` scaled by `gain` with hard saturation (`b` itself when `gain <= 1`).
pub fn gain_pcm(b: &[u8], gain: i64) -> std::borrow::Cow<'_, [u8]> {
    if gain <= 1 {
        return std::borrow::Cow::Borrowed(b);
    }
    let n = b.len() & !1;
    let mut out = vec![0u8; n];
    let mut i = 0;
    while i + 1 < n {
        let v = i64::from(i16::from_le_bytes([b[i], b[i + 1]])) * gain;
        let v = v.clamp(-32768, 32767) as i16;
        out[i..i + 2].copy_from_slice(&v.to_le_bytes());
        i += 2;
    }
    std::borrow::Cow::Owned(out)
}

/// `exec.LookPath`: the path of the executable `name` names. A name with a `/` is used as it is;
/// otherwise each directory of `PATH` is searched. The error text is Go's. Differences
/// (PARITY.md 128): an executable is recognised by its mode bits (Go also asks `access(2)`), and
/// an empty `PATH` entry means the current directory, which Go refuses the same way
/// (`ErrDot`).
pub fn look_path(name: &str) -> Result<PathBuf, String> {
    count_lookup(name);
    look_path_in(name, &std::env::var_os("PATH").unwrap_or_default())
}

/// [`look_path`] on the blocking pool: it stats every `PATH` directory, and a hung network
/// mount in one of them must not stall the runtime (DESIGN.md: nothing blocks inside a task).
pub async fn look_path_blocking(name: &str) -> Result<PathBuf, String> {
    let n = name.to_string();
    match tokio::task::spawn_blocking(move || look_path(&n)).await {
        Ok(r) => r,
        Err(e) => Err(format!("looking up the binary: {e}")),
    }
}

/// Test seam: the names whose lookups are counted (a test counts its own unique path, so the
/// parallel tests of the binary do not disturb the count).
#[cfg(feature = "test-seams")]
static LOOKUPS: std::sync::Mutex<Vec<(String, usize)>> = std::sync::Mutex::new(Vec::new());

#[cfg(not(feature = "test-seams"))]
fn count_lookup(_name: &str) {}

#[cfg(feature = "test-seams")]
fn count_lookup(name: &str) {
    let mut g = lock(&LOOKUPS);
    if let Some(e) = g.iter_mut().find(|(n, _)| n == name) {
        e.1 += 1;
    }
}

/// Starts counting the lookups of `name` (from zero).
#[cfg(feature = "test-seams")]
pub fn count_lookups_of(name: &str) {
    let mut g = lock(&LOOKUPS);
    g.retain(|(n, _)| n != name);
    g.push((name.to_string(), 0));
}

/// How many times `name` was looked up since [`count_lookups_of`].
#[cfg(feature = "test-seams")]
pub fn lookups_of(name: &str) -> usize {
    let g = lock(&LOOKUPS);
    g.iter().find(|(n, _)| n == name).map_or(0, |e| e.1)
}

/// [`look_path`] over a given `PATH` value. An EMPTY `PATH` has no entries (Go's
/// `filepath.SplitList("")` is empty); `std::env::split_paths("")` yields one empty entry.
pub fn look_path_in(name: &str, path: &std::ffi::OsStr) -> Result<PathBuf, String> {
    let err = |cause: String| format!("exec: {}: {}", crate::gostd::strconv::go_quote(name), cause);
    if name.contains('/') {
        return match find_executable(Path::new(name)) {
            Ok(()) => Ok(PathBuf::from(name)),
            Err(c) => Err(err(c)),
        };
    }
    let dirs = if path.is_empty() {
        Vec::new()
    } else {
        std::env::split_paths(path).collect()
    };
    for dir in dirs {
        let dir = if dir.as_os_str().is_empty() {
            PathBuf::from(".")
        } else {
            dir
        };
        let cand = dir.join(name);
        if find_executable(&cand).is_ok() {
            if !cand.is_absolute() {
                return Err(err(
                    "cannot run executable found relative to current directory".to_string(),
                ));
            }
            return Ok(cand);
        }
    }
    Err(err("executable file not found in $PATH".to_string()))
}

fn find_executable(p: &Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    let md = std::fs::metadata(p)
        .map_err(|e| format!("stat {}: {}", p.display(), crate::gojson::io_err(&e)))?;
    if md.is_dir() {
        return Err("is a directory".to_string());
    }
    if md.permissions().mode() & 0o111 != 0 {
        Ok(())
    } else {
        Err("permission denied".to_string())
    }
}

/// How Go prints the error of `cmd.Wait` for a process that ended badly: `exit status 3`,
/// `signal: killed`. `None` for a clean exit (Go's nil).
pub fn exit_text(st: &std::process::ExitStatus) -> Option<String> {
    use std::os::unix::process::ExitStatusExt;
    if st.success() {
        return None;
    }
    if let Some(c) = st.code() {
        return Some(format!("exit status {c}"));
    }
    st.signal().map(|s| {
        // Go: "signal: " + syscall.Signal.String(), then " (core dumped)"
        let mut t = format!("signal: {}", signal_name(s));
        if st.core_dumped() {
            t.push_str(" (core dumped)");
        }
        t
    })
}

/// `syscall.Signal.String()` on Linux (`zerrors_linux_amd64.go`): the name of signals 1 to 31,
/// `signal N` for any other.
fn signal_name(s: i32) -> String {
    const NAMES: [&str; 31] = [
        "hangup",
        "interrupt",
        "quit",
        "illegal instruction",
        "trace/breakpoint trap",
        "aborted",
        "bus error",
        "floating point exception",
        "killed",
        "user defined signal 1",
        "segmentation fault",
        "user defined signal 2",
        "broken pipe",
        "alarm clock",
        "terminated",
        "stack fault",
        "child exited",
        "continued",
        "stopped (signal)",
        "stopped",
        "stopped (tty input)",
        "stopped (tty output)",
        "urgent I/O condition",
        "CPU time limit exceeded",
        "file size limit exceeded",
        "virtual timer expired",
        "profiling timer expired",
        "window changed",
        "I/O possible",
        "power failure",
        "bad system call",
    ];
    match usize::try_from(s) {
        Ok(n) if (1..=NAMES.len()).contains(&n) => NAMES[n - 1].to_string(),
        _ => format!("signal {s}"),
    }
}

/// Test seam: the next time the code at one of these places runs, it panics (once).
#[cfg(feature = "test-seams")]
pub static PANIC_NEXT: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
pub const PANIC_DOWNLINK: u32 = 1;
pub const PANIC_UPLINK: u32 = 2;
pub const PANIC_PUMP: u32 = 4;
pub const PANIC_TEARDOWN: u32 = 8;

/// Test seam: the next spawn at one of these places fails (once).
#[cfg(feature = "test-seams")]
static FAIL_NEXT: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
#[cfg(feature = "test-seams")]
pub const FAIL_DOWNLINK_SPAWN: u32 = 1;

/// Arms [`FAIL_NEXT`] for `bit`.
#[cfg(feature = "test-seams")]
pub fn fail_next_for_test(bit: u32) {
    FAIL_NEXT.fetch_or(bit, std::sync::atomic::Ordering::SeqCst);
}

/// True once if `bit` was armed (and disarms it).
#[cfg(feature = "test-seams")]
pub(crate) fn take_failure(bit: u32) -> bool {
    use std::sync::atomic::Ordering::{Relaxed, SeqCst};
    FAIL_NEXT.load(Relaxed) & bit != 0 && FAIL_NEXT.fetch_and(!bit, SeqCst) & bit != 0
}

/// Arms [`PANIC_NEXT`] for `bit`.
#[cfg(feature = "test-seams")]
pub fn panic_next_for_test(bit: u32) {
    PANIC_NEXT.fetch_or(bit, std::sync::atomic::Ordering::SeqCst);
}

/// A shipped build has no injection hook: this is empty and the call sites cost nothing.
#[cfg(not(feature = "test-seams"))]
#[inline(always)]
pub fn maybe_panic(_bit: u32) {}

#[cfg(feature = "test-seams")]
pub fn maybe_panic(bit: u32) {
    use std::sync::atomic::Ordering::{Relaxed, SeqCst};
    if PANIC_NEXT.load(Relaxed) & bit != 0 && PANIC_NEXT.fetch_and(!bit, SeqCst) & bit != 0 {
        panic!("test panic {bit} carrying SECRET-API-KEY and rtsp://u:p@h/x");
    }
}

/// Runs `fut` as a task of its own and logs, scrubbed, how it ended if it panicked. The caller
/// keeps a flag in a drop guard when it must be set however the task ends.
pub fn spawn_guarded<F>(what: &'static str, secret: String, fut: F)
where
    F: std::future::Future<Output = ()> + Send + 'static,
{
    let inner = tokio::spawn(fut);
    tokio::spawn(async move {
        if let Err(je) = inner.await {
            if je.is_panic() {
                let text = crate::util::panic_message(&*je.into_panic(), "<non-string payload>");
                crate::panic_hook::log_line(&format!(
                    "portico-transport: {what}: a task panicked: {}",
                    scrub_all(&text, &secret)
                ));
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    // TestScrubLineAndGainHelpers (talk_down_test.go): the scrub half is in process/scrub.rs
    #[test]
    fn gain_helpers() {
        let input = [1u8, 0];
        let out = gain_pcm(&input, 1);
        assert!(
            matches!(out, std::borrow::Cow::Borrowed(_)),
            "gain 1 copied"
        );
    }

    #[test]
    fn gain_saturates_and_copies() {
        let b = [100, 0, 0xFF, 0x7F, 0x00, 0x80, 7];
        let o = gain_pcm(&b, 4);
        assert_eq!(&o[..], &[0x90, 0x01, 0xFF, 0x7F, 0x00, 0x80]); // the odd byte is dropped
        assert_eq!(&gain_pcm(&b, 0)[..], &b[..]);
    }

    #[test]
    fn ffmpeg_args_and_sinks() {
        let a = ffmpeg_args("rtp://192.168.2.9:7004", "opus", 24000).unwrap();
        assert_eq!(
            a.join(" "),
            "-hide_banner -loglevel error -f s16le -ar 16000 -ac 1 -i pipe:0 -c:a libopus -application voip -ar 24000 -ac 1 -f rtp rtp://192.168.2.9:7004"
        );
        assert_eq!(
            ffmpeg_args("rtp://h:1", "aac", 24000).unwrap_err(),
            "talkback codec \"aac\" is not supported (only opus)"
        );
        for (raw, ok) in [
            ("rtp://192.168.2.9:7004", true),
            ("udp://h:1?x=1", true),
            ("file:///etc/passwd", false),
            ("http://evil.example/x", false),
            ("rtp://", false),
            ("", false),
            ("rtp://h:1/a b", false),
            ("rtp://h:1/a\nb", false),
            ("-rtp://h", false),
        ] {
            assert_eq!(valid_sink(raw).is_ok(), ok, "{raw:?}");
        }
        assert_eq!(
            valid_sink("file://h/x").unwrap_err(),
            "talkback sink scheme \"file\" is not allowed"
        );
        // no host: Go's first check wins
        assert_eq!(
            valid_sink("file:///x").unwrap_err(),
            "talkback sink is not a usable URL"
        );
        // DEV-796 D2 (PARITY.md 474): loopback, unspecified, multicast (v4, v6, mapped) are
        // refused; a LAN or public camera address and a plain name still pass.
        for bad in [
            "rtp://127.0.0.1:5060",
            "rtp://127.9.9.9:1",
            "udp://[::1]:7004",
            "rtp://0.0.0.0:7004",
            "rtp://[::]:7004",
            "rtp://224.0.0.1:7004",
            "rtp://239.1.2.3:7004",
            "rtp://[ff02::1]:7004",
            "rtp://[::ffff:127.0.0.1]:7004",
            "rtp://localhost:7004",
            "rtp://LOCALHOST.:7004",
            "rtp://127.0.0.1.:5060",
            "rtp://foo.localhost:7004",
            "rtp://ip6-localhost:7004",
            "rtp://ip6-loopback:7004",
            "rtp://127.1:5060",
            "rtp://127.0.1:5060",
            "rtp://2130706433:5060",
            "rtp://0x7f000001:5060",
            "rtp://0x7f.0.0.1:5060",
            "rtp://0177.0.0.1:5060",
            "rtp://0:7004",
            "rtp://0xe0000001:7004",
            "rtp://::1",
            "rtp://::",
            "rtp://ff02::1",
            "rtp://[::127.0.0.1]:7004",
            "rtp://[::1]:5060[]:7004",
            "rtp://[::1][10.0.0.1]:7004",
            "rtp://u@127.0.0.1:5060@10.0.0.1:7004",
            "rtp://0x00000000000000007f000001:5060",
            "rtp://0000000000000000000000177.0.0.1:5060",
            "rtp://[::7f00:1]:7004",
        ] {
            assert_eq!(
                valid_sink(bad).unwrap_err(),
                "talkback sink host is not allowed",
                "{bad}"
            );
        }
        for good in [
            "rtp://10.8.0.20:7004",
            "rtp://169.254.1.1:7004",
            "rtp://[fe80::1]:7004",
            "rtp://[2001:db8::1]:7004",
            "rtp://camera.lan:7004",
            "rtp://8.8.8.8:7004",
            "rtp://10.1.2.3.:7004",
            "rtp://localhost.example.com:7004",
            "rtp://127.example.com:7004",
            "rtp://notlocalhost:7004",
            "rtp://0x0a000001:7004",
            "rtp://u:p@10.1.2.3:7004",
            "rtp://[fe80::1]:7004",
            "rtp://[::10.0.0.1]:7004",
        ] {
            assert!(valid_sink(good).is_ok(), "{good}");
        }
        assert_eq!(
            valid_sink("rtp://").unwrap_err(),
            "talkback sink is not a usable URL"
        );
        assert_eq!(
            downlink_args("rtsp://h/x").join(" "),
            "-hide_banner -loglevel error -rtsp_transport tcp -i rtsp://h/x -vn -ac 1 -ar 16000 -f s16le pipe:1"
        );
    }

    #[test]
    fn a_long_codec_text_is_capped_in_the_error() {
        let long = "x".repeat(10_000);
        let e = ffmpeg_args("rtp://h:1", &long, 24000).unwrap_err();
        assert!(e.len() < 400 && e.contains("..."), "{}", e.len());
        // a cut inside a multi-byte character backs up to its start
        let multi = "é".repeat(300);
        let e = ffmpeg_args("rtp://h:1", &multi, 24000).unwrap_err();
        assert!(e.len() < 400 && e.contains("..."));
        // short texts are Go's, untouched
        assert_eq!(
            ffmpeg_args("rtp://h:1", "aac", 1).unwrap_err(),
            "talkback codec \"aac\" is not supported (only opus)"
        );
    }

    #[test]
    fn look_path_texts() {
        assert_eq!(
            look_path("/nonexistent/ffmpeg").unwrap_err(),
            "exec: \"/nonexistent/ffmpeg\": stat /nonexistent/ffmpeg: no such file or directory"
        );
        assert_eq!(
            look_path("/tmp").unwrap_err(),
            "exec: \"/tmp\": is a directory"
        );
        assert!(look_path("/bin/sh").is_ok());
        assert_eq!(
            look_path("no-such-binary-xyzzy").unwrap_err(),
            "exec: \"no-such-binary-xyzzy\": executable file not found in $PATH"
        );
    }

    #[test]
    fn exit_texts() {
        use std::os::unix::process::ExitStatusExt;
        let s = |raw| std::process::ExitStatus::from_raw(raw);
        assert_eq!(exit_text(&s(0)), None);
        assert_eq!(exit_text(&s(3 << 8)).as_deref(), Some("exit status 3"));
        assert_eq!(exit_text(&s(9)).as_deref(), Some("signal: killed"));
        assert_eq!(exit_text(&s(15)).as_deref(), Some("signal: terminated"));
        // Go's names for the rest of the table, and the core-dump suffix
        assert_eq!(
            exit_text(&s(4)).as_deref(),
            Some("signal: illegal instruction")
        );
        assert_eq!(
            exit_text(&s(5)).as_deref(),
            Some("signal: trace/breakpoint trap")
        );
        assert_eq!(
            exit_text(&s(10)).as_deref(),
            Some("signal: user defined signal 1")
        );
        assert_eq!(
            exit_text(&s(12)).as_deref(),
            Some("signal: user defined signal 2")
        );
        assert_eq!(
            exit_text(&s(31)).as_deref(),
            Some("signal: bad system call")
        );
        assert_eq!(exit_text(&s(34)).as_deref(), Some("signal: signal 34"));
        assert_eq!(
            exit_text(&s(11 | 0x80)).as_deref(),
            Some("signal: segmentation fault (core dumped)")
        );
        assert_eq!(
            exit_text(&s(6 | 0x80)).as_deref(),
            Some("signal: aborted (core dumped)")
        );
    }

    /// R1-94: Go splits an empty `PATH` into no directories, so a `./name` in the current
    /// directory is not even looked at (and is no `ErrDot`); `:` is two empty entries.
    #[test]
    fn an_empty_path_has_no_directories() {
        use std::os::unix::fs::PermissionsExt;
        struct Rm(PathBuf);
        impl Drop for Rm {
            fn drop(&mut self) {
                let _ = std::fs::remove_file(&self.0);
            }
        }
        let name = format!("portico-r1-94-probe-{}", std::process::id());
        let p = PathBuf::from(&name);
        let _rm = Rm(p.clone());
        std::fs::write(&p, "#!/bin/sh\n").unwrap();
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert_eq!(
            look_path_in(&name, std::ffi::OsStr::new("")).unwrap_err(),
            format!("exec: \"{name}\": executable file not found in $PATH")
        );
        assert_eq!(
            look_path_in(&name, std::ffi::OsStr::new(":")).unwrap_err(),
            format!("exec: \"{name}\": cannot run executable found relative to current directory")
        );
    }
}
