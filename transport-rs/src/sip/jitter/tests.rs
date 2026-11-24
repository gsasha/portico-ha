use super::*;
use tokio::time::{sleep, timeout};

const MS: fn(u64) -> Duration = Duration::from_millis;

async fn next(jb: &JitterBuffer, what: &str) -> Vec<u8> {
    timeout(Duration::from_secs(2), jb.recv())
        .await
        .unwrap_or_else(|_| panic!("timed out waiting for {what}"))
        .unwrap()
}

// TestJitterBuffer_ReordersOutOfOrderPackets
#[tokio::test(start_paused = true)]
async fn reorders_out_of_order_packets() {
    let jb = JitterBuffer::new(MS(10), MS(30), 4, 0);
    // Pushed out of order: 2, 0, 1 - must play out as 0, 1, 2.
    jb.push(2, vec![2; 4]);
    jb.push(0, vec![0; 4]);
    jb.push(1, vec![1; 4]);
    for i in 0..3u8 {
        assert_eq!(next(&jb, "a frame").await, vec![i; 4], "frame {i}");
    }
    jb.close();
}

// TestJitterBuffer_FillsGapWithSilence
#[tokio::test(start_paused = true)]
async fn fills_gap_with_silence() {
    let jb = JitterBuffer::new(MS(10), MS(30), 4, 0);
    jb.push(0, vec![0; 4]);
    jb.push(2, vec![2; 4]); // seq 1 is never pushed - lost packet
    assert_eq!(next(&jb, "frame 0").await, vec![0; 4]);
    assert_eq!(
        next(&jb, "the silence slot").await,
        vec![0; 4],
        "lost = silence"
    );
    assert_eq!(next(&jb, "frame 2").await, vec![2; 4]);
    jb.close();
}

// TestJitterBuffer_DropsPacketsArrivingTooLate
#[tokio::test(start_paused = true)]
async fn drops_packets_arriving_too_late() {
    let jb = JitterBuffer::new(MS(10), MS(20), 4, 0);
    jb.push(0, vec![0; 4]);
    next(&jb, "seq 0").await; // next_seq is now 1
                              // seq 0 again, absurdly late (a duplicate/stale packet) must not play twice.
    jb.push(0, vec![0xff; 4]);
    jb.push(1, vec![1; 4]);
    assert_eq!(
        next(&jb, "seq 1").await,
        vec![1; 4],
        "next frame must be seq 1's real payload, not the late duplicate"
    );
    // R2-17: the stale packet is PURGED, not just skipped: without the purge it stays in the map
    // (unbounded for a peer that repeats packets) and plays when the 16-bit sequence comes round.
    let left: Vec<u16> = jb.state().buf.keys().copied().collect();
    assert!(left.is_empty(), "{left:?}");
    assert_eq!(jb.stats().late_discarded, 1);
    jb.close();
}

// TestSeqBefore_HandlesWraparound
#[test]
fn seq_before_handles_wraparound() {
    for (a, b, want) in [
        (0u16, 1u16, true),
        (1, 0, false),
        (65535, 0, true),
        (0, 65535, false),
        (100, 100, false),
    ] {
        assert_eq!(seq_before(a, b), want, "seqBefore({a}, {b})");
    }
}

// TestJitterBuffer_Admit (one case per Go subtest)
#[test]
fn admit() {
    // ptime 10ms, delay 30ms -> window is RESYNC_MIN_GAP (8) frames.
    struct C(&'static str, u16, &'static [u16], u16, usize);
    let cases = [
        C(
            "normal reorder",
            100,
            &[103, 101, 102, 100, 99, 104],
            100,
            6,
        ),
        C(
            "reorder across wraparound",
            65534,
            &[1, 65535, 0, 65534, 2],
            65534,
            5,
        ),
        C(
            "stray far packet is ignored",
            100,
            &[5000, 100, 101],
            100,
            2,
        ),
        C(
            "base far ahead resyncs",
            100,
            &[30000, 30001, 30002, 30003, 30004],
            30004,
            1,
        ),
        C(
            "base far behind resyncs",
            30000,
            &[100, 101, 102, 103, 104],
            104,
            1,
        ),
        C(
            "base across wraparound resyncs",
            100,
            &[40000, 40001, 40002, 40003, 40004],
            40004,
            1,
        ),
        C(
            "scattered outliers never resync",
            100,
            &[5000, 20000, 40000, 60000, 9000, 30000],
            100,
            0,
        ),
        C(
            "outliers interrupted by in-window packet",
            100,
            &[5000, 5001, 5002, 5003, 101, 5004],
            100,
            1,
        ),
    ];
    for C(name, next, pushes, want_next, want_kept) in cases {
        let jb = JitterBuffer::new(MS(10), MS(30), 4, 0);
        let mut st = jb.state();
        st.playing = true;
        st.next_seq = next;
        for &s in pushes {
            // lock is held by `st`; Inner::admit does not take it
            if jb.admit_for_test(&mut st, s) {
                st.buf.insert(s, vec![0]);
            }
        }
        assert_eq!(
            (st.next_seq, st.buf.len()),
            (want_next, want_kept),
            "{name}: nextSeq/kept"
        );
    }
}

// TestJitterBuffer_RecoversFromSequenceJump
#[tokio::test(start_paused = true)]
async fn recovers_from_sequence_jump() {
    // new base ahead of the first stream (20000); 40000 is rewritten to 100 (behind)
    for base0 in [30000u16, 40000] {
        let jb = JitterBuffer::new(MS(10), MS(30), 1, 0);
        let first = 20000u16;
        for i in 0..5u16 {
            jb.push(first + i, vec![1]);
        }
        for _ in 0..5 {
            next(&jb, "first stream").await;
        }
        // Stream restarts with a new base; keep sending at ptime.
        let base = if base0 == 40000 { 100 } else { base0 };
        let jb2 = jb.clone();
        tokio::spawn(async move {
            for i in 0..60u16 {
                jb2.push(base + i, vec![7]);
                sleep(MS(10)).await;
            }
        });
        let deadline = Instant::now() + Duration::from_secs(2);
        let mut got = false;
        while !got {
            let f = timeout(deadline - Instant::now(), jb.recv())
                .await
                .unwrap_or_else(|_| {
                    panic!("base {base}: never recovered audio after sequence jump")
                })
                .unwrap();
            got = f[0] == 7;
        }
        jb.close();
    }
}

#[tokio::test(start_paused = true)]
async fn close_ends_recv() {
    let jb = JitterBuffer::new(MS(10), MS(30), 4, 0);
    let j2 = jb.clone();
    let t = tokio::spawn(async move { j2.recv().await });
    sleep(MS(5)).await;
    jb.close();
    assert_eq!(t.await.unwrap(), None);
}

// R1-103: a push after close is not stored (it would be admitted for good inside the
// initial delay window).
#[tokio::test(start_paused = true)]
async fn push_after_close_is_ignored() {
    let jb = JitterBuffer::new(MS(40), MS(80), 4, 0);
    jb.close();
    for seq in 0..100u16 {
        jb.push(seq, vec![1; 4]);
    }
    assert!(jb.state().buf.is_empty());
}

// DEV-796 S8: before playout starts, distinct sequence numbers must not grow the buffer without
// bound (65536 keys x ~2 KB).
#[tokio::test(start_paused = true)]
async fn pre_playout_buffer_is_capped() {
    let jb = JitterBuffer::new(MS(20), MS(80), 4, 0);
    for seq in 0..=u16::MAX {
        jb.push(seq, vec![0; 4]);
    }
    let n = jb.state().buf.len();
    assert!(n <= 256, "buf grew to {n}");
    jb.close();
}
