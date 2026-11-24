//! The `dahua_sip` driver against a fake station on loopback: the tests of `dahua_test.go` (the Go
//! name is in a comment on each) and the behaviours this port adds. Nothing here reaches anything
//! but 127.0.0.1.

#![allow(unsafe_code)] // a libc call in a test helper (kill / signal); the library has none

mod common;

use std::sync::Arc;
use std::time::Duration;

use common::dahua::*;
use serde_json::{Map, Value};
use tokio::net::UdpSocket;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::dahua::{Dahua, STOP_TIMEOUT};
use transport_rs::driver::{Driver, EventSink, MediaTarget, PendingRinger};

fn ms(n: u64) -> Duration {
    Duration::from_millis(n)
}

fn cfg() -> Map<String, Value> {
    serde_json::from_str(r#"{"host":"127.0.0.1","sip_user":"8001#1","sip_password":"pw"}"#).unwrap()
}

fn driver(port: u16) -> Dahua {
    let mut d = Dahua::default();
    d.set_sip_port(port as i64);
    d.configure(&cfg()).unwrap();
    d
}

async fn started(port: u16, ev: Arc<dyn EventSink>) -> Arc<Dahua> {
    let d = Arc::new(driver(port));
    d.start(&CancelToken::new(), ev).await.unwrap();
    d
}

fn target(port: i64) -> MediaTarget {
    MediaTarget {
        host: "127.0.0.1".into(),
        port,
    }
}

// TestStop_SendsCleanDeregisterBeforeReturning
#[tokio::test]
async fn stop_sends_clean_deregister_before_returning() {
    let st = DStation::new().await;
    let d = started(st.port(), Rec::new()).await;
    assert!(
        st.wait_regs(ms(3000)).await,
        "device never saw an authenticated REGISTER"
    );
    d.stop().await.unwrap();
    assert_eq!(
        st.counts().1,
        1,
        "the device must have seen one Expires: 0 REGISTER by the time stop returned"
    );
}

// TestStop_DoesNotHangOnAnUnreachableDevice
#[tokio::test]
async fn stop_does_not_hang_on_an_unreachable_device() {
    let silent = UdpSocket::bind("127.0.0.1:0").await.unwrap(); // listens but never answers
    let d = started(silent.local_addr().unwrap().port(), Rec::new()).await;
    let t0 = std::time::Instant::now();
    tokio::time::timeout(STOP_TIMEOUT + ms(4000), d.stop())
        .await
        .expect("stop hung on a device that never answers")
        .unwrap();
    assert!(
        t0.elapsed() >= ms(2500),
        "stop did not wait for the de-register"
    );
}

// TestBye_ReportsEnded
#[tokio::test]
async fn bye_reports_ended() {
    let st = DStation::new().await;
    let ev = Rec::new();
    let d = started(st.port(), ev.clone()).await;
    assert!(st.wait_regs(ms(3000)).await);
    st.send(&bye("call-xyz", "bye1")).await;
    assert!(
        ev.wait("state|call-xyz|ended", ms(3000)).await,
        "BYE did not produce an ended event"
    );
    let got = st.collect(ms(200)).await;
    assert!(
        got.iter()
            .any(|m| m.starts_with("SIP/2.0 200") && m.contains("CSeq: 2 BYE")),
        "{got:?}"
    );
    d.stop().await.unwrap();
}

// TestInviteRetransmitIsDeduped_AndToTagIsStable
#[tokio::test]
async fn invite_retransmit_is_deduped_and_to_tag_is_stable() {
    let st = DStation::new().await;
    let ev = Rec::new();
    let d = started(st.port(), ev.clone()).await;
    assert!(st.wait_regs(ms(3000)).await);
    st.send(&invite("ring-1", "inv1", true)).await;
    assert!(st.wait_for("SIP/2.0 180", ms(2000)).await, "no 180");
    let first = st.collect(ms(100)).await;
    let tag = to_tag_of(&first[0]);
    assert!(!tag.is_empty(), "180 carries no To-tag");
    assert!(ev.wait("ringing|ring-1|127.0.0.1", ms(1000)).await);
    let orig = d.peek_pending_ring().unwrap();

    st.send(&invite("ring-1", "inv1", true)).await; // retransmission
    assert!(st.wait_for("SIP/2.0 180", ms(2000)).await);
    let again = st.collect(ms(100)).await;
    assert!(
        first_line(&again[0]).contains("180") && to_tag_of(&again[0]) == tag,
        "{again:?}"
    );
    assert_eq!(
        ev.count("ringing"),
        1,
        "a retransmitted INVITE fired Ringing again"
    );
    assert_eq!(
        d.peek_pending_ring().unwrap().to_tag,
        orig.to_tag,
        "a retransmitted INVITE replaced the pending ring"
    );

    d.answer(&CancelToken::new(), target(5000)).await.unwrap();
    assert!(st.wait_for("SIP/2.0 200", ms(2000)).await);
    let ok = st.collect(ms(100)).await;
    assert_eq!(
        to_tag_of(&ok[0]),
        tag,
        "the 200 must carry the ring-time tag"
    );
    st.send(&invite("ring-1", "inv1", true)).await; // late retransmit after answer: ignored
    let late = st.collect(ms(300)).await;
    assert!(
        late.is_empty(),
        "the late retransmit was answered: {late:?}"
    );
    assert_eq!(
        ev.count("ringing"),
        1,
        "INVITE retransmit after Answer rang again"
    );
    d.stop().await.unwrap();
}

// TestCancel_ClearsTheRing
#[tokio::test]
async fn cancel_clears_the_ring() {
    let st = DStation::new().await;
    let ev = Rec::new();
    let d = started(st.port(), ev.clone()).await;
    assert!(st.wait_regs(ms(3000)).await);
    st.send(&invite("ring-1", "inv1", true)).await;
    assert!(st.wait_for("SIP/2.0 180", ms(2000)).await);
    let tag = to_tag_of(&st.collect(ms(50)).await[0]);
    st.send(&cancel("ring-1", "inv1")).await;
    assert!(
        ev.wait("state|ring-1|ended", ms(3000)).await,
        "CANCEL did not produce an ended event"
    );
    let got = st.collect(ms(100)).await;
    let c200 = got
        .iter()
        .find(|m| first_line(m) == "SIP/2.0 200 OK")
        .expect("no 200");
    let i487 = got
        .iter()
        .find(|m| first_line(m) == "SIP/2.0 487 Request Terminated")
        .expect("no 487");
    assert!(c200.contains("CSeq: 1 CANCEL") && i487.contains("CSeq: 1 INVITE"));
    assert_eq!(to_tag_of(i487), tag);
    assert!(
        d.pending_ring().is_none(),
        "the cancelled ring is still pending"
    );
    assert!(d
        .answer(&CancelToken::new(), MediaTarget::default())
        .await
        .is_err());

    // R2-119: the cancelled ring's Call-ID stays known: a late retransmit of the INVITE is not a
    // new ring (no `ringing`, no 180)
    let before = ev.events().len();
    st.send(&invite("ring-1", "inv1", true)).await;
    assert!(
        st.collect(ms(150)).await.is_empty(),
        "a late INVITE retransmit after the CANCEL was answered"
    );
    assert_eq!(ev.events().len(), before, "a late INVITE rang again");

    // a CANCEL for a call that is not ringing gets 481 and no event
    let before = ev.events().len();
    st.send(&cancel("ring-1", "inv1")).await;
    assert!(st.wait_for("SIP/2.0 481", ms(2000)).await);
    st.collect(ms(100)).await;
    assert_eq!(
        ev.events().len(),
        before,
        "a stray CANCEL produced an event"
    );
    d.stop().await.unwrap();
}

// TestStart_RejectedCredentialsReportedAsDriverError
#[tokio::test]
async fn rejected_credentials_are_reported_as_driver_error() {
    let st = DStation::with("403", "none").await;
    let ev = Rec::new();
    let d = started(st.port(), ev.clone()).await;
    assert!(
        ev.wait("error|", ms(3000)).await,
        "no driver_error: {:?}",
        ev.events()
    );
    let evs = ev.events();
    assert_eq!(
        evs.iter()
            .filter(|e| e.starts_with("avail|127.0.0.1|false|"))
            .count(),
        1,
        "{evs:?}"
    );
    assert_eq!(
        ev.count("error|127.0.0.1|registration_failed|"),
        1,
        "{evs:?}"
    );
    d.stop().await.unwrap();
}

// R2-32 (dahua.go:185-200, `reported && wasRegistered == registered`): availability is reported on
// TRANSITIONS only. A station that grants one second makes the registrar refresh every ~0.5 s;
// three successful REGISTERs still make exactly one `avail ... true`.
#[tokio::test]
async fn availability_is_reported_on_transitions_only() {
    let st = DStation::with("expires1", "none").await;
    let ev = Rec::new();
    let d = started(st.port(), ev.clone()).await;
    let end = std::time::Instant::now() + ms(6000);
    while st.counts().0 < 3 && std::time::Instant::now() < end {
        tokio::time::sleep(ms(20)).await;
    }
    assert!(st.counts().0 >= 3, "only {} REGISTERs", st.counts().0);
    assert_eq!(
        ev.events(),
        vec!["avail|127.0.0.1|true|".to_string()],
        "one report for several refreshes"
    );
    d.stop().await.unwrap();
}

// TestStart_RegistrationReportedAvailable (the capability half: ReportsAvailability)
#[tokio::test]
async fn registration_is_reported_available() {
    let st = DStation::new().await;
    let ev = Rec::new();
    let d = driver(st.port());
    assert!(
        d.capabilities().reports_availability,
        "ReportsAvailability must be true"
    );
    let d = Arc::new(d);
    d.start(&CancelToken::new(), ev.clone()).await.unwrap();
    assert!(ev.wait("avail|", ms(3000)).await, "no availability report");
    assert_eq!(ev.events(), vec!["avail|127.0.0.1|true|".to_string()]);
    d.stop().await.unwrap();
}

// ---- behaviours this port adds (PARITY.md differences 98 and following) --------------------------

#[tokio::test]
async fn a_second_start_is_refused_and_leaves_the_first_running() {
    let st = DStation::new().await;
    let d = started(st.port(), Rec::new()).await;
    let e = d.start(&CancelToken::new(), Rec::new()).await.unwrap_err();
    assert_eq!(e.message(), "dahua_sip: already started");
    assert!(st.wait_regs(ms(3000)).await);
    d.stop().await.unwrap();
    assert_eq!(st.counts().1, 1);
}

#[tokio::test]
async fn dial_before_start_and_after_stop_is_an_error_not_a_crash() {
    let d = driver(1);
    let e = d.dial(&CancelToken::new(), target(6000)).await.unwrap_err();
    assert_eq!(e.message(), "dahua_sip: Dial: the driver is not started");
    let st = DStation::new().await;
    let d = started(st.port(), Rec::new()).await;
    d.stop().await.unwrap();
    let e = d.dial(&CancelToken::new(), target(6000)).await.unwrap_err();
    assert!(
        e.message()
            .starts_with("dahua_sip: Dial: sip: writing request"),
        "{}",
        e.message()
    );
    // a second stop is a no-op
    assert!(d.stop().await.is_ok());
}

#[tokio::test]
async fn a_panic_in_the_registrar_is_reported_unavailable_and_the_registrar_restarts() {
    let st = DStation::new().await;
    let ev = Rec::new();
    let d = driver(st.port());
    d.panic_in_registrar_for_test();
    d.set_restart_delay(ms(150));
    let d = Arc::new(d);
    d.start(&CancelToken::new(), ev.clone()).await.unwrap();
    assert!(
        ev.wait("avail|127.0.0.1|false|internal error", ms(3000))
            .await,
        "the panicked device stayed available: {:?}",
        ev.events()
    );
    // started again: registers, and is reported available again
    assert!(
        st.wait_regs(ms(4000)).await,
        "the registrar was not restarted"
    );
    assert!(
        ev.wait("avail|127.0.0.1|true|", ms(2000)).await,
        "{:?}",
        ev.events()
    );
    let t0 = std::time::Instant::now();
    d.stop().await.unwrap();
    assert!(t0.elapsed() < ms(1500));
    assert_eq!(
        st.counts(),
        (1, 1),
        "one registration and one clean de-register"
    );
}

/// Stop during the wait before a restart ends it at once: nothing registers afterwards.
#[tokio::test]
async fn stop_during_the_restart_wait_ends_the_registrar_for_good() {
    let st = DStation::new().await;
    let ev = Rec::new();
    let d = driver(st.port());
    d.panic_in_registrar_for_test();
    d.set_restart_delay(Duration::from_secs(30));
    let d = Arc::new(d);
    d.start(&CancelToken::new(), ev.clone()).await.unwrap();
    assert!(
        ev.wait("avail|127.0.0.1|false|internal error", ms(3000))
            .await
    );
    let t0 = std::time::Instant::now();
    d.stop().await.unwrap();
    assert!(t0.elapsed() < ms(1500), "stop waited out the restart delay");
    assert_eq!(st.counts(), (0, 0));
}

#[tokio::test]
async fn the_hubs_context_ending_deregisters_without_a_stop() {
    let st = DStation::new().await;
    let ctx = CancelToken::new();
    let d = Arc::new(driver(st.port()));
    d.start(&ctx, Rec::new()).await.unwrap();
    assert!(st.wait_regs(ms(3000)).await);
    ctx.cancel();
    let end = tokio::time::Instant::now() + ms(3000);
    while st.counts().1 == 0 && tokio::time::Instant::now() < end {
        tokio::time::sleep(ms(20)).await;
    }
    assert_eq!(
        st.counts().1,
        1,
        "the account stayed registered after the context ended"
    );
    d.stop().await.unwrap();
}

/// A sink with no `errorReporter` extension: the driver finds none and the registration failure
/// is only logged (Go: the type assertion fails).
#[tokio::test]
async fn a_sink_without_the_error_extension_is_fine() {
    struct Plain(std::sync::Mutex<Vec<String>>);
    impl EventSink for Plain {
        fn ringing(&self, _: &str, _: &str) {}
        fn state_changed(&self, _: &str, _: &str) {}
        fn availability_changed(&self, id: &str, ok: bool, _: &str) {
            self.0.lock().unwrap().push(format!("{id}:{ok}"));
        }
    }
    let st = DStation::with("403", "none").await;
    let p = Arc::new(Plain(Default::default()));
    let d = started(st.port(), p.clone()).await;
    let end = tokio::time::Instant::now() + ms(3000);
    while p.0.lock().unwrap().is_empty() && tokio::time::Instant::now() < end {
        tokio::time::sleep(ms(20)).await;
    }
    assert_eq!(*p.0.lock().unwrap(), vec!["127.0.0.1:false".to_string()]);
    d.stop().await.unwrap();
}

/// A BYE for a call that is not the ringing one leaves the ring alone and still reports `ended`
/// for the id it named (Go reports any BYE).
#[tokio::test]
async fn a_bye_for_another_call_leaves_the_ring_and_is_reported() {
    let st = DStation::new().await;
    let ev = Rec::new();
    let d = started(st.port(), ev.clone()).await;
    assert!(st.wait_regs(ms(3000)).await);
    st.send(&invite("ring-1", "inv1", true)).await;
    assert!(ev.wait("ringing|ring-1", ms(2000)).await);
    st.send(&bye("other", "bye9")).await;
    assert!(ev.wait("state|other|ended", ms(2000)).await);
    assert!(
        d.pending_ring_for("ring-1").is_some(),
        "the ring went with another call's BYE"
    );
    d.stop().await.unwrap();
}

/// A flood of retransmits of one INVITE is one ring and (while the ring is pending) one 180 each:
/// nothing accumulates in the driver.
#[tokio::test]
async fn a_flood_of_invite_retransmits_is_one_ring() {
    let st = DStation::new().await;
    let ev = Rec::new();
    let d = started(st.port(), ev.clone()).await;
    assert!(st.wait_regs(ms(3000)).await);
    for _ in 0..300 {
        st.send(&invite("flood-1", "inv1", true)).await;
    }
    tokio::time::sleep(ms(600)).await;
    assert_eq!(ev.count("ringing"), 1, "{:?}", ev.events().len());
    assert!(d.peek_pending_ring().is_some());
    d.stop().await.unwrap();
}

#[tokio::test]
async fn the_registry_builds_a_dahua_driver_that_configures_like_go() {
    let reg = transport_rs::driver::registry();
    let mut d = reg["dahua_sip"]();
    let e = d.configure(&Map::new()).unwrap_err();
    assert_eq!(
        e.message(),
        "dahua_sip: host, sip_user and sip_password are required"
    );
    d.configure(&cfg()).unwrap();
    assert!(
        d.as_pending_ringer().is_some(),
        "the pendingRinger assertion must succeed"
    );
    assert!(d.as_call_hangupper().is_none());
    assert!(d.as_connect_answerer().is_none());
    assert!(d.as_audio_claimer().is_none());
    assert!(
        d.device_id().is_none(),
        "dahua has no DeviceID: the registry keys it by host"
    );
}

fn is_hex16(s: &str) -> bool {
    s.len() == 16
        && s.bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

/// The golden masking hides the SHAPE of what the code mints; pin it: a ring's To-tag is 16 hex,
/// our INVITE's branch is `z9hG4bK` + 16 hex, its From-tag 16 hex and its Call-ID `hex16@host`.
#[tokio::test]
async fn minted_tokens_have_gos_shapes() {
    let st = DStation::with("ok", "200").await;
    let ev = Rec::new();
    let d = started(st.port(), ev.clone()).await;
    assert!(st.wait_regs(ms(3000)).await);
    st.send(&invite("ring-1", "inv1", true)).await;
    assert!(st.wait_for("SIP/2.0 180", ms(2000)).await);
    let tag = to_tag_of(&st.collect(ms(50)).await[0]);
    assert!(is_hex16(&tag), "To-tag {tag:?}");
    d.dial(&CancelToken::new(), target(6000)).await.unwrap();
    let inv = st
        .collect(ms(200))
        .await
        .into_iter()
        .find(|m| m.starts_with("INVITE "))
        .unwrap();
    let via = header(&inv, "Via");
    let br = via.split("branch=").nth(1).unwrap();
    assert!(
        br.starts_with("z9hG4bK") && is_hex16(&br[7..]),
        "branch {br:?}"
    );
    let ft = header(&inv, "From")
        .split("tag=")
        .nth(1)
        .unwrap()
        .to_string();
    assert!(is_hex16(&ft), "From-tag {ft:?}");
    let cid = header(&inv, "Call-ID");
    let (l, r) = cid.split_once('@').unwrap();
    assert!(is_hex16(l) && r == "127.0.0.1", "Call-ID {cid:?}");
    d.stop().await.unwrap();
}

// ---- Phase R round 1, group F ---------------------------------------------------------------

/// R1-97: `stop` ends the run, so a `start` on the same instance opens a new endpoint (Go does);
/// a second `start` WITHOUT a stop is still refused.
#[tokio::test]
async fn a_start_after_a_stop_opens_a_new_endpoint() {
    let st = DStation::new().await;
    let d = started(st.port(), Rec::new()).await;
    assert!(st.wait_regs(ms(3000)).await);
    d.stop().await.unwrap();
    assert_eq!(st.counts(), (1, 1));
    d.start(&CancelToken::new(), Rec::new())
        .await
        .expect("a start after a stop was refused");
    let end = tokio::time::Instant::now() + ms(4000);
    while st.counts().0 < 2 && tokio::time::Instant::now() < end {
        tokio::time::sleep(ms(20)).await;
    }
    assert_eq!(st.counts().0, 2, "the second run never registered");
    let e = d.start(&CancelToken::new(), Rec::new()).await.unwrap_err();
    assert_eq!(e.message(), "dahua_sip: already started");
    d.stop().await.unwrap();
    assert_eq!(st.counts(), (2, 2));
}

/// R1-32: the supervisor sets `done` however it ends, a panic of its own included, so `stop`
/// never waits out `STOP_TIMEOUT` for a latch nobody will set.
#[tokio::test]
async fn a_panic_of_the_supervisor_itself_does_not_make_stop_wait() {
    let st = DStation::new().await;
    let d = driver(st.port());
    d.panic_in_supervisor_for_test();
    let d = Arc::new(d);
    d.start(&CancelToken::new(), Rec::new()).await.unwrap();
    assert!(st.wait_regs(ms(3000)).await);
    let t0 = std::time::Instant::now();
    d.stop().await.unwrap();
    assert!(
        t0.elapsed() < STOP_TIMEOUT / 2,
        "stop waited {:?} for a supervisor that was gone",
        t0.elapsed()
    );
    assert_eq!(st.counts(), (1, 1), "the clean de-register went out");
}

/// R1-32 / R1-63: the log lines of the driver never panic on a closed stdout or stderr
/// (`println!` does). The test runs itself in a child process whose stdout and stderr are
/// /dev/full (a write fails) and reads what the child wrote to a file.
#[tokio::test]
async fn the_driver_logs_survive_a_failing_stdout_and_stderr() {
    const CHILD: &str = "PORTICO_R1_32_CHILD";
    if let Ok(marker) = std::env::var(CHILD) {
        // the child: from here on every write to stdout and stderr fails (/dev/full on 1 and 2;
        // a closed descriptor would be handed to the next socket and the log would go into it).
        // The test harness itself may panic on its own output afterwards: only the marker counts.
        unsafe {
            let full = libc::open(c"/dev/full".as_ptr(), libc::O_WRONLY);
            assert!(full >= 0 && libc::dup2(full, 1) >= 0 && libc::dup2(full, 2) >= 0);
        }
        let st = DStation::new().await;
        let d = driver(st.port());
        d.panic_in_registrar_for_test(); // a panic line on stderr as well
        d.set_restart_delay(ms(50));
        let d = Arc::new(d);
        d.start(&CancelToken::new(), Rec::new()).await.unwrap();
        let ok = st.wait_regs(ms(5000)).await;
        let t0 = std::time::Instant::now();
        d.stop().await.unwrap();
        let fast = t0.elapsed() < STOP_TIMEOUT / 2;
        let deregistered = st.counts().1 == 1;
        std::fs::write(
            marker,
            format!("registered={ok} fast_stop={fast} deregistered={deregistered}"),
        )
        .unwrap();
        return;
    }
    let dir = std::env::temp_dir().join(format!("portico-r1-32-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let marker = dir.join("marker");
    let mut cmd = std::process::Command::new(std::env::current_exe().unwrap());
    cmd.args([
        "--exact",
        "the_driver_logs_survive_a_failing_stdout_and_stderr",
        "--nocapture",
        "--test-threads=1",
    ])
    .env(CHILD, &marker)
    .stdin(std::process::Stdio::null())
    .stdout(std::process::Stdio::null())
    .stderr(std::process::Stdio::null());
    let _ = cmd.status().expect("the child test binary");
    let got = std::fs::read_to_string(&marker).unwrap_or_default();
    let _ = std::fs::remove_dir_all(&dir);
    assert_eq!(
        got, "registered=true fast_stop=true deregistered=true",
        "the child (stdout and stderr failing) did not get through"
    );
}

/// R1-135: a registrar that ran for `stable_after` before it panicked starts the backoff afresh;
/// a quick panic doubles it.
#[test]
fn the_restart_wait_starts_again_after_a_long_run() {
    use transport_rs::driver::dahua::restart_plan;
    let (base, stable) = (Duration::from_secs(5), Duration::from_secs(120));
    let quick = Duration::from_secs(1);
    assert_eq!(restart_plan(0, quick, base, stable), (1, base));
    assert_eq!(restart_plan(1, quick, base, stable), (2, base * 2));
    assert_eq!(restart_plan(3, quick, base, stable), (4, base * 8));
    // capped at five minutes
    assert_eq!(
        restart_plan(20, quick, base, stable),
        (21, Duration::from_secs(300))
    );
    // a run of the stable time (or more) before the panic: a first failure again
    assert_eq!(restart_plan(7, stable, base, stable), (1, base));
    assert_eq!(restart_plan(7, stable * 5, base, stable), (1, base));
    assert_eq!(
        restart_plan(7, stable - Duration::from_millis(1), base, stable).0,
        8
    );
}

// DEV-796 D3: the SIP password is scrubbed from any panic text once configured.
#[test]
fn configure_registers_the_sip_password_for_scrubbing() {
    let mut d = Dahua::default();
    let mut c = cfg();
    c.insert("sip_password".into(), "dahua-sip-secret-7731".into());
    d.configure(&c).unwrap();
    assert_eq!(
        transport_rs::process::panic_hook::scrub("boom dahua-sip-secret-7731 end"),
        "boom *** end"
    );
}

// DEV-796 D3: the password is registered even when the configuration is refused for another
// field.
#[test]
fn a_refused_configure_still_registers_the_sip_password() {
    let mut d = Dahua::default();
    let mut c = cfg();
    c.insert("host".into(), "".into());
    c.insert("sip_password".into(), "refused-sip-secret-5521".into());
    assert!(d.configure(&c).is_err());
    assert_eq!(
        transport_rs::process::panic_hook::scrub("x refused-sip-secret-5521 y"),
        "x *** y"
    );
}
