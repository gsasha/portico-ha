//! The session task: the select loop that feeds the core its commands, datagrams, timers and device audio.

use super::*;

/// The most datagrams one pass of the loop takes before the device audio, the timers and the
/// commands get their turn (R2-19). Small enough that a pass is far shorter than a 40 ms frame,
/// large enough that a burst of the phone's own RTP does not cost a pass each.
const UDP_BATCH: usize = 32;

pub(super) async fn run(
    mut core: Core,
    mut cmd_rx: mpsc::Receiver<Cmd>,
    mut udp_rx: mpsc::Receiver<UdpEv>,
    udp_tx: mpsc::Sender<UdpEv>,
    mut audio_rx: mpsc::Receiver<Vec<u8>>,
) {
    let stop = core.stop.clone();
    core.log("session created; waiting for the phone's offer");
    tokio::spawn(report::lag_sampler(core.shared.clone(), stop.clone()));
    let mut every = tokio::time::interval_at(
        tokio::time::Instant::now() + report::REPORT_EVERY,
        report::REPORT_EVERY,
    );
    every.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut prev = report::Snap::default();
    let mut prev_at = Instant::now();
    let end = loop {
        // a panicked or stopped session does not poll its `Rtc` again (R1-20)
        if core.panicked {
            break End::Panic;
        }
        if stop.is_cancelled() {
            break End::Stopped;
        }
        let timeout = match core.drain() {
            Ok(t) => t,
            Err(e) => break e,
        };
        if core.capped {
            // let the other tasks, and `stop`, in before the next pass
            tokio::task::yield_now().await;
        }
        match core.guard("is_alive", |c| c.rtc.is_alive()) {
            Ok(true) => {}
            Ok(false) => break End::PeerClosed,
            Err(e) => break e,
        }
        if core.have_offer && core.cfg.fault_is(Fault::PanicInTask) {
            panic!("injected panic in the session task");
        }
        let deadline = core.next_deadline(timeout);
        let r: Result<(), End> = tokio::select! {
            biased;
            _ = stop.cancelled() => break End::Stopped,
            cmd = cmd_rx.recv() => match cmd {
                None => break End::Stopped,
                Some(Cmd::Offer(sdp, reply)) => {
                    core.log(&format!(
                        "offer received ({} bytes) +{}ms",
                        sdp.len(),
                        core.since_start_ms()
                    ));
                    let r = core.offer(sdp, &udp_tx).await;
                    match &r {
                        Ok(a) => core.log(&format!(
                            "answer sent ({} bytes, payload types {:?}) +{}ms",
                            a.len(),
                            core.answer_pts,
                            core.since_start_ms()
                        )),
                        Err(e) => core.log(&format!("offer refused: {e} +{}ms", core.since_start_ms())),
                    }
                    let _ = reply.send(r);
                    Ok(())
                }
                Some(Cmd::Answer(sdp, reply)) => {
                    let r = match sdp::pion_first_line_error(&sdp) {
                        Some(e) => Err(format!("sfu: setting remote answer: failed to unmarshal SDP: {e}")),
                        None => Err("sfu: setting remote answer: InvalidModificationError: invalid proposed signaling state transition: stable->SetRemote(answer)->stable".to_string()),
                    };
                    let _ = reply.send(r);
                    Ok(())
                }
                Some(Cmd::Candidate(c, reply)) => {
                    let r = core.candidate(&c);
                    let _ = reply.send(r);
                    Ok(())
                }
            },
            _ = every.tick() => {
                let cur = report::Snap::take(&core.shared, core.path.jitter());
                let st = &core.shared.stats;
                // only a call whose phone has sent audio has an uplink to report
                if cur.rtp_rx > 0 || prev.rtp_rx > 0 {
                    let line = report::window_line(
                        &prev,
                        &cur,
                        prev_at.elapsed().as_millis(),
                        core.path.jitter().depth(),
                        st.out_depth_window_max.swap(0, Ordering::Relaxed) as usize,
                        core.path.jitter().jitter_stats().max_tick_lag_ms,
                        st.lag_window_max_ms.swap(0, Ordering::Relaxed),
                    );
                    core.log(&line);
                }
                prev = cur;
                prev_at = Instant::now();
                Ok(())
            },
            // before the datagrams, so a flood of garbage cannot starve str0m's timers (keepalives)
            _ = tokio::time::sleep_until(tokio::time::Instant::from_std(deadline)) => core.step("a timer", |c| c.on_timer()),
            ev = udp_rx.recv() => match ev {
                None => break End::Stopped,
                Some(ev) => serve_datagrams(&mut core, ev, &mut udp_rx, &mut audio_rx),
            },
            f = audio_rx.recv() => match f {
                None => break End::Stopped,
                Some(f) => core.step("writing audio", |c| c.write_audio(f)),
            },
        };
        if let Err(e) = r {
            break e;
        }
    };
    finish(&mut core, end).await;
}

/// What one pass of the loop does with a datagram and with a device frame (the core in production, a
/// counter in the test of R3A-3).
trait Pass {
    fn datagram(&mut self, ev: UdpEv) -> Result<(), End>;
    fn audio(&mut self, f: Vec<u8>) -> Result<(), End>;
}

impl Pass for Core {
    fn datagram(&mut self, ev: UdpEv) -> Result<(), End> {
        Core::datagram(self, ev)
    }
    fn audio(&mut self, f: Vec<u8>) -> Result<(), End> {
        self.step("writing audio", |c| c.write_audio(f))
    }
}

/// One pass of the datagram arm (R2-19): this arm is ahead of the device audio, and with several
/// sockets feeding the one queue a flood keeps it non-empty, so the audio arm would never be
/// polled (measured: 0 of 75 frames, 4 addresses). Hence a bounded batch of datagrams per pass
/// (`first` and at most `UDP_BATCH - 1` more), then ONE device frame taken without waiting: the
/// audio gets a turn after at most `UDP_BATCH` datagrams.
fn serve_datagrams<P: Pass>(
    p: &mut P,
    first: UdpEv,
    udp_rx: &mut mpsc::Receiver<UdpEv>,
    audio_rx: &mut mpsc::Receiver<Vec<u8>>,
) -> Result<(), End> {
    let mut r = p.datagram(first);
    for _ in 1..UDP_BATCH {
        if r.is_err() {
            break;
        }
        match udp_rx.try_recv() {
            Ok(ev) => r = p.datagram(ev),
            Err(_) => break,
        }
    }
    if r.is_ok() {
        if let Ok(f) = audio_rx.try_recv() {
            r = p.audio(f);
        }
    }
    r
}

/// The end of a session, in the order of `Close`: say why, stop the helpers, close str0m, then
/// `OnDisconnected`; the sockets are released when this returns.
async fn finish(core: &mut Core, end: End) {
    core.log(&format!(
        "session ending: {end:?} +{}ms (ever connected: {})",
        core.since_start_ms(),
        core.ever_connected
    ));
    match end {
        End::Grace => {
            core.peer_state("failed");
            core.log("Disconnected did not recover within the grace; closing");
        }
        End::ConnectTimeout => {
            core.peer_state("failed");
            core.log("the browser never connected; closing");
        }
        End::Panic => core.log("str0m panicked; the session is closed"),
        End::PeerClosed | End::Stopped => {}
    }
    // order as Close: stop the helpers, close str0m, then OnDisconnected
    core.stop.cancel();
    core.path.jitter().close();
    // str0m's graceful close runs under the same guard and is bounded, so it is tried after a
    // panic too
    core.goodbye();
    {
        let mut g = lock(&core.shared.cb);
        g.closed = true;
    }
    {
        let st = &core.shared.stats;
        core.log(&format!(
            "uplink: rtp_rx={} udp_dropped={} udp_tx_dropped={} jitter_out_dropped={} audio_out_frames={} audio_out_dropped={} concealed={} decoder_resets={} rtp_gap_ms_max={} rtp_gap_ms{:?}={:?} loop_lag_ms_max={} jitter={:?}",
            st.rtp_rx.load(Ordering::Relaxed),
            st.udp_dropped.load(Ordering::Relaxed),
            st.udp_tx_dropped.load(Ordering::Relaxed),
            core.path.jitter().dropped(),
            st.audio_out_frames.load(Ordering::Relaxed),
            st.audio_out_dropped.load(Ordering::Relaxed),
            st.plc_slots.load(Ordering::Relaxed),
            st.decoder_resets.load(Ordering::Relaxed),
            st.gap_max_ms.load(Ordering::Relaxed),
            report::RTP_GAP_BOUNDS_MS,
            st.gap_bins.iter().map(|b| b.load(Ordering::Relaxed)).collect::<Vec<_>>(),
            st.lag_max_ms.load(Ordering::Relaxed),
            core.path.jitter().jitter_stats()
        ));
    }
    if core.path.foreign() > 0 {
        core.log(&format!(
            "dropped {} RTP packets that were not G.722/PCMU",
            core.path.foreign()
        ));
    }
    core.peer_state("closed");
    core.shared.fire_disconnected();
    core.socks.clear();
    // the readers see `stop` and let go of their sockets: `wait_done` means they are released
    for r in core.readers.drain(..) {
        let _ = r.await;
    }
    core.shared.stats.teardown.store(true, Ordering::SeqCst);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct Count {
        datagrams: usize,
        frames: usize,
    }

    impl Pass for Count {
        fn datagram(&mut self, _: UdpEv) -> Result<(), End> {
            self.datagrams += 1;
            Ok(())
        }
        fn audio(&mut self, _: Vec<u8>) -> Result<(), End> {
            self.frames += 1;
            Ok(())
        }
    }

    fn ev() -> UdpEv {
        UdpEv {
            idx: 0,
            src: "127.0.0.1:9".parse().unwrap(),
            data: vec![0x80; 12],
        }
    }

    // R3A-3, PARITY 441: with far more than `UDP_BATCH` datagrams queued and a device frame
    // waiting, one pass takes at most `UDP_BATCH` datagrams and then ONE frame; a second pass
    // takes the next batch (and the next frame). Mutants, one test each: the batch loop without the
    // frame (`frames == 0`), the loop unbounded (`datagrams == 100`), a frame per datagram.
    #[test]
    fn one_pass_takes_a_batch_of_datagrams_and_then_one_device_frame() {
        let (utx, mut urx) = mpsc::channel(256);
        let (atx, mut arx) = mpsc::channel(8);
        for _ in 0..100 {
            utx.try_send(ev()).unwrap();
        }
        for _ in 0..3 {
            atx.try_send(vec![0u8; 4]).unwrap();
        }
        let mut c = Count::default();
        let first = urx.try_recv().unwrap();
        serve_datagrams(&mut c, first, &mut urx, &mut arx).unwrap();
        assert_eq!(c.datagrams, UDP_BATCH, "a pass takes at most one batch");
        assert_eq!(c.frames, 1, "and then ONE device frame");
        let first = urx.try_recv().unwrap();
        serve_datagrams(&mut c, first, &mut urx, &mut arx).unwrap();
        assert_eq!((c.datagrams, c.frames), (2 * UDP_BATCH, 2));
    }

    // a short queue ends the batch early; with no frame waiting none is written
    #[test]
    fn a_short_queue_ends_the_batch_and_no_frame_is_invented() {
        let (utx, mut urx) = mpsc::channel(8);
        let (_atx, mut arx) = mpsc::channel::<Vec<u8>>(8);
        utx.try_send(ev()).unwrap();
        utx.try_send(ev()).unwrap();
        let mut c = Count::default();
        let first = urx.try_recv().unwrap();
        serve_datagrams(&mut c, first, &mut urx, &mut arx).unwrap();
        assert_eq!((c.datagrams, c.frames), (2, 0));
    }
}
