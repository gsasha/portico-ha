//! R1-31: a media open that fails at the downlink must not leave `session_up` true. The failure
//! is injected through a process-wide seam (`fail_next_for_test`), so this binary has ONE test.

mod common;

use std::time::Duration;

use common::talk::*;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::unifiprotect::ffmpeg::{fail_next_for_test, FAIL_DOWNLINK_SPAWN};
use transport_rs::driver::{ConnectAnswerer, Driver, ExternalRinger};

#[tokio::test]
async fn an_open_that_fails_at_the_downlink_leaves_no_session_up() {
    let r = Rig::new().await;
    r.d.external_ring("c1", Duration::from_secs(60)).unwrap();
    fail_next_for_test(FAIL_DOWNLINK_SPAWN);
    let e =
        r.d.answer_with_audio(&CancelToken::new(), "c1")
            .await
            .err()
            .expect("the downlink could not start");
    assert!(
        e.message()
            .starts_with("unifi_protect_talk: starting the downlink ffmpeg: "),
        "{}",
        e.message()
    );
    // the uplink was started and closed again: nothing is up
    assert!(!r.d.health().session_up, "session_up stayed true");
    // and the call can be answered again
    r.d.answer_with_audio(&CancelToken::new(), "c1")
        .await
        .expect("a second answer");
    r.d.stop().await.unwrap();
}
