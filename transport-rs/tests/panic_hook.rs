//! The scrubbing panic hook (slice 7b review, PARITY.md 131): the default hook prints the payload
//! of a panic before it unwinds, so a secret in a panic text would reach stderr. This test runs a
//! CHILD process (this test binary again, selected by an environment variable) that registers an
//! API key through a real driver `configure`, installs the hook, and panics four ways with the
//! key and a URL in the text (a `&str`, a `String`, the library's own `maybe_panic` seam, each
//! inside `catch_unwind` as the media tasks are, and a last uncontained one); the parent reads
//! the child's whole stderr and asserts neither the key nor a URL's userinfo appears and that the
//! fixed `panic at` line does.

use std::process::Command;

use serde_json::json;
use transport_rs::driver::unifiprotect::ffmpeg::{maybe_panic, panic_next_for_test, PANIC_PUMP};
use transport_rs::driver::unifiprotect::talk::TalkDriver;
use transport_rs::driver::Driver;

const KEY: &str = "TOPSECRET-KEY-12345";

#[test]
fn child_that_panics_with_a_secret() {
    if std::env::var("PORTICO_PANIC_CHILD").is_err() {
        return; // the parent test below runs this one in a subprocess
    }
    transport_rs::panic_hook::install();
    // the seam's own text names this literal; a driver registers its key at `configure`
    transport_rs::panic_hook::register_secret("SECRET-API-KEY");
    let mut d = TalkDriver::default();
    d.configure(
        json!({"device_id": "d", "protect_host": "h", "protect_api_key": KEY, "camera_id": "c"})
            .as_object()
            .unwrap(),
    )
    .unwrap();
    let r =
        std::panic::catch_unwind(|| panic!("a str payload with {KEY} and rtsp://user:{KEY}@h/x"));
    assert!(r.is_err());
    let text = format!("a String payload with {KEY} and rtsp://user:pw@h/x");
    let r = std::panic::catch_unwind(move || std::panic::panic_any(text));
    assert!(r.is_err());
    panic_next_for_test(PANIC_PUMP);
    let r = std::panic::catch_unwind(|| maybe_panic(PANIC_PUMP));
    assert!(r.is_err());
    // and a panic that is not contained, last: the process dies with the scrubbed report
    panic!("final {KEY} rtsps://a:b@c/d");
}

#[test]
fn a_panic_never_prints_a_secret() {
    if std::env::var("PORTICO_PANIC_CHILD").is_ok() {
        return;
    }
    let out = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "child_that_panics_with_a_secret",
            "--nocapture",
            "--test-threads=1",
        ])
        .env("PORTICO_PANIC_CHILD", "1")
        .output()
        .unwrap();
    let err = String::from_utf8_lossy(&out.stderr);
    assert!(
        !out.status.success(),
        "the final, uncontained panic must end the child"
    );
    for bad in [
        KEY,
        "user:",
        "a:b@",
        "SECRET-API-KEY",
        "rtsp://",
        "rtsps://",
    ] {
        assert!(!err.contains(bad), "{bad:?} reached stderr:\n{err}");
    }
    let n = err.matches("portico-transport: panic at ").count();
    assert_eq!(n, 4, "four panics, four fixed lines:\n{err}");
    assert!(err.contains("a str payload with *** and <url>"), "{err}");
    assert!(err.contains("final *** <url>"), "{err}");
}

// main.rs installs the hook before anything else
#[test]
fn main_installs_the_hook_first() {
    if std::env::var("PORTICO_PANIC_CHILD").is_ok() {
        return;
    }
    let src = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/src/main.rs")).unwrap();
    let i = src.find("fn main() {").unwrap();
    let body = &src[i..];
    assert!(body.find("panic_hook::install()").unwrap() < body.find("flags::parse").unwrap());
}
