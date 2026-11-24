//! The WebRTC call's audio counters over time (D-20261121-ee09): every `REPORT_EVERY` the session
//! logs what changed in that window (not only a total at the end), so a degradation can be placed
//! in the call. Everything here is plain arithmetic over snapshots, so the line is unit-tested
//! without a session.

use super::*;

/// The period of the windowed line.
pub(super) const REPORT_EVERY: Duration = Duration::from_secs(5);
/// How often the loop-lag sampler wakes (one 20 ms RTP slot).
pub(super) const LAG_EVERY: Duration = Duration::from_millis(20);
/// Upper bounds (ms) of the phone's RTP inter-arrival histogram; packets come every 20 ms
/// nominally (G.722) so 25 is "on time". 8 bins, the last open-ended.
pub const RTP_GAP_BOUNDS_MS: [u64; 7] = [25, 40, 60, 100, 200, 400, 1000];
/// Upper bounds (ms) of the session loop-lag histogram (how late the sampler woke); 9 bins.
pub const LAG_BOUNDS_MS: [u64; 8] = [5, 10, 20, 40, 80, 160, 320, 640];

pub(super) fn bin(bounds: &[u64], ms: u64) -> usize {
    bounds.iter().position(|b| ms <= *b).unwrap_or(bounds.len())
}

/// The upper bound of the bin where `bins` reaches 95 % of its samples, capped by `max`
/// (`None` when there are no samples).
pub(super) fn p95(bins: &[u64], bounds: &[u64], max: u64) -> Option<u64> {
    let total: u64 = bins.iter().sum();
    if total == 0 {
        return None;
    }
    let need = (total * 95).div_ceil(100);
    let mut cum = 0;
    for (i, n) in bins.iter().enumerate() {
        cum += n;
        if cum >= need {
            return Some(bounds.get(i).map_or(max, |b| (*b).min(max)));
        }
    }
    Some(max)
}

/// Everything the windowed line differences (cumulative values).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(super) struct Snap {
    pub rtp_rx: u64,
    pub udp_dropped: u64,
    pub udp_tx_dropped: u64,
    pub jitter_out_dropped: u64,
    pub audio_out_frames: u64,
    pub audio_out_dropped: u64,
    pub concealed: u64,
    pub decoder_resets: u64,
    pub silence_slots: u64,
    pub late_discarded: u64,
    pub outliers_refused: u64,
    pub resyncs: u64,
    pub gaps: [u64; 8],
    pub lag: [u64; 9],
}

impl Snap {
    pub(super) fn take(shared: &Shared, jitter: &UplinkBuffer) -> Snap {
        let st = &shared.stats;
        let g = |a: &AtomicU64| a.load(Ordering::Relaxed);
        let j = jitter.jitter_stats();
        Snap {
            rtp_rx: g(&st.rtp_rx),
            udp_dropped: g(&st.udp_dropped),
            udp_tx_dropped: g(&st.udp_tx_dropped),
            jitter_out_dropped: jitter.dropped(),
            audio_out_frames: g(&st.audio_out_frames),
            audio_out_dropped: g(&st.audio_out_dropped),
            concealed: g(&st.plc_slots),
            decoder_resets: g(&st.decoder_resets),
            silence_slots: j.silence_slots,
            late_discarded: j.late_discarded,
            outliers_refused: j.outliers_refused,
            resyncs: j.resyncs,
            gaps: std::array::from_fn(|i| g(&st.gap_bins[i])),
            lag: std::array::from_fn(|i| g(&st.lag_bins[i])),
        }
    }
}

fn d<const N: usize>(a: &[u64; N], b: &[u64; N]) -> [u64; N] {
    std::array::from_fn(|i| a[i].saturating_sub(b[i]))
}

/// The windowed line: counter deltas since `prev`, the arrival-gap bins of the window, queue
/// depths now (`jitter_held` frames in the jitter buffer + `Out`) and the window's maxima
/// (`out_depth_max`, `tick_lag_max_ms`, `lag_max_ms`). A fixed set of fields, so it stays short.
pub(super) fn window_line(
    prev: &Snap,
    cur: &Snap,
    window_ms: u128,
    jitter_depth: usize,
    out_depth_max: usize,
    tick_lag_max_ms: u64,
    lag_max_ms: u64,
) -> String {
    let lag = d(&cur.lag, &prev.lag);
    let lag_p95 = p95(&lag, &LAG_BOUNDS_MS, lag_max_ms).unwrap_or(0);
    let dd = |a: u64, b: u64| a.saturating_sub(b);
    format!(
        "uplink window {window_ms}ms: rtp_rx=+{} udp_dropped=+{} udp_tx_dropped=+{} jitter_out_dropped=+{} audio_out_frames=+{} audio_out_dropped=+{} silence_slots=+{} late_discarded=+{} outliers_refused=+{} resyncs=+{} concealed=+{} decoder_resets=+{} rtp_gap_ms{:?}={:?} jitter_depth={} out_depth_max={} tick_lag_ms_max_call={} loop_lag_ms_max={} loop_lag_ms_p95={}",
        dd(cur.rtp_rx, prev.rtp_rx),
        dd(cur.udp_dropped, prev.udp_dropped),
        dd(cur.udp_tx_dropped, prev.udp_tx_dropped),
        dd(cur.jitter_out_dropped, prev.jitter_out_dropped),
        dd(cur.audio_out_frames, prev.audio_out_frames),
        dd(cur.audio_out_dropped, prev.audio_out_dropped),
        dd(cur.silence_slots, prev.silence_slots),
        dd(cur.late_discarded, prev.late_discarded),
        dd(cur.outliers_refused, prev.outliers_refused),
        dd(cur.resyncs, prev.resyncs),
        dd(cur.concealed, prev.concealed),
        dd(cur.decoder_resets, prev.decoder_resets),
        RTP_GAP_BOUNDS_MS,
        d(&cur.gaps, &prev.gaps),
        jitter_depth,
        out_depth_max,
        tick_lag_max_ms,
        lag_max_ms,
        lag_p95,
    )
}

/// The delivery-side settings of the call, logged once when the uplink starts (and in the last
/// line), so a number is never read without the settings it was taken under.
pub(super) fn settings_line(ptime: Duration, delay: Duration) -> String {
    format!(
        "uplink settings: ptime_ms={} jitter_delay_ms={} resync_window_frames={} report_every_s={}",
        ptime.as_millis(),
        delay.as_millis(),
        (3 * delay.as_nanos() / ptime.as_nanos().max(1)).max(8),
        REPORT_EVERY.as_secs()
    )
}

/// Wakes every `LAG_EVERY` and records how late it was: a busy runtime delays every frame
/// through it. Ends with `stop`.
pub(super) async fn lag_sampler(shared: Arc<Shared>, stop: CancelToken) {
    let mut t = tokio::time::interval(LAG_EVERY);
    t.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    t.tick().await;
    loop {
        tokio::select! {
            _ = stop.cancelled() => return,
            at = t.tick() => {
                let late = tokio::time::Instant::now().saturating_duration_since(at).as_millis() as u64;
                let st = &shared.stats;
                st.lag_bins[bin(&LAG_BOUNDS_MS, late)].fetch_add(1, Ordering::Relaxed);
                st.lag_max_ms.fetch_max(late, Ordering::Relaxed);
                st.lag_window_max_ms.fetch_max(late, Ordering::Relaxed);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn p95_reads_the_bin_where_95_percent_is_reached() {
        assert_eq!(p95(&[0; 9], &LAG_BOUNDS_MS, 0), None);
        // 20 samples, one in the 160..320 bin: ceil(19) reached in bin 0 -> 5
        let mut b = [0u64; 9];
        b[0] = 19;
        b[6] = 1;
        assert_eq!(p95(&b, &LAG_BOUNDS_MS, 300), Some(5));
        b[0] = 18;
        b[6] = 2;
        assert_eq!(p95(&b, &LAG_BOUNDS_MS, 300), Some(300), "capped by the max");
        assert_eq!(bin(&RTP_GAP_BOUNDS_MS, 25), 0);
        assert_eq!(bin(&RTP_GAP_BOUNDS_MS, 26), 1);
        assert_eq!(bin(&RTP_GAP_BOUNDS_MS, 5000), 7);
    }

    #[test]
    fn the_window_line_is_a_delta_with_every_field() {
        let mut prev = Snap::default();
        prev.rtp_rx = 100;
        prev.silence_slots = 5;
        prev.gaps[0] = 90;
        let mut cur = prev.clone();
        cur.rtp_rx = 350;
        cur.silence_slots = 12;
        cur.late_discarded = 3;
        cur.concealed = 7;
        cur.gaps[0] = 300;
        cur.gaps[4] = 2;
        cur.lag[0] = 250;
        let l = window_line(&prev, &cur, 5000, 6, 3, 2, 4);
        for want in [
            "window 5000ms",
            "rtp_rx=+250",
            "silence_slots=+7",
            "late_discarded=+3",
            "concealed=+7",
            "decoder_resets=+0",
            "[25, 40, 60, 100, 200, 400, 1000]=[210, 0, 0, 0, 2, 0, 0, 0]",
            "jitter_depth=6",
            "out_depth_max=3",
            "tick_lag_ms_max_call=2",
            "loop_lag_ms_max=4",
            "loop_lag_ms_p95=4",
        ] {
            assert!(l.contains(want), "{want} not in {l}");
        }
        assert!(l.len() < 600, "bounded: {}", l.len());
    }

    #[test]
    fn the_settings_line_states_delay_and_resync_window() {
        let l = settings_line(Duration::from_millis(20), Duration::from_millis(140));
        assert!(l.contains("ptime_ms=20 jitter_delay_ms=140 resync_window_frames=21"), "{l}");
    }
}
