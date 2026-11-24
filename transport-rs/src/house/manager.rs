//! `Manager`: the table of bridges (the cap, the per-owner cap, `open` / `close` / `stop`) and the `OpenGuard` that frees a half-built slot.

use super::bridge::{Bridge, BuiltLeg, LegState};
use super::{
    mono_now, random_hex, BoxFut, Config, EmitFn, Event, Factory, FrameError, HouseError, HouseLeg,
    LegSpec, Opened, OpenedLeg, WebRtcLeg, DEFAULT_CALL_PREFIX, DEFAULT_MAX_BRIDGES,
    DEFAULT_MAX_PER_OWNER, DEFAULT_SILENCE, KIND_FRAMES, KIND_WEBRTC, MAX_CALL_ID_LEN,
    MAX_OWNER_LEN, REASON_HANGUP, REASON_LOST, REASON_SHUTDOWN, SESSION_PREFIX, SIDE_FROM, SIDE_TO,
};
use crate::cancel::CancelToken;
use crate::util::lock;
use std::collections::HashMap;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::AtomicU64;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;
use tokio::sync::mpsc;
use tokio::sync::oneshot;

#[derive(Default)]
pub(super) struct Tables {
    pub(super) closed: bool,
    /// call id -> session id of a slot reserved while its legs are built
    pub(super) pending: HashMap<String, String>,
    pub(super) by_call: HashMap<String, Arc<Bridge>>,
    pub(super) by_session: HashMap<String, Arc<Bridge>>,
    pub(super) by_leg: HashMap<String, (Arc<Bridge>, usize)>,
    pub(super) owners: HashMap<String, usize>,
}

/// The house-owned table of bridges.
pub struct Manager {
    pub(super) cfg: Config,
    pub(super) f: Arc<dyn Factory>,
    /// the events, each with the ack of whoever awaits its delivery (see `emit_delivered`)
    pub(super) ev_tx: mpsc::UnboundedSender<(Event, Option<oneshot::Sender<()>>)>,
    pub(super) t: Mutex<Tables>,
}

/// The index of a side in a bridge's `legs` (`from` 0, `to` 1); `None` for any other text.
pub(super) fn side_index(side: &str) -> Option<usize> {
    match side {
        SIDE_FROM => Some(0),
        SIDE_TO => Some(1),
        _ => None,
    }
}

/// Runs `f` under a panic guard (no task of its own): a panic in it is logged and swallowed.
pub(super) async fn guarded(what: &str, f: BoxFut) {
    crate::guard::logged(&format!("house: {what}"), f).await;
}

impl Manager {
    /// Builds an empty manager. `emit` receives the `house_leg_state` events one at a time, in
    /// order, from one task (never with a lock held). Needs a tokio runtime.
    pub fn new(cfg: Config, f: Arc<dyn Factory>, emit: EmitFn) -> Arc<Manager> {
        let mut cfg = cfg;
        if cfg.max_bridges == 0 {
            cfg.max_bridges = DEFAULT_MAX_BRIDGES;
        }
        if cfg.max_per_owner == 0 {
            cfg.max_per_owner = DEFAULT_MAX_PER_OWNER;
        }
        if cfg.silence.is_zero() {
            cfg.silence = DEFAULT_SILENCE;
        }
        if cfg.close_all_wait.is_zero() {
            cfg.close_all_wait = Duration::from_secs(6);
        }
        if cfg.call_prefix.is_empty() {
            cfg.call_prefix = DEFAULT_CALL_PREFIX.to_string();
        }
        let (ev_tx, mut ev_rx) = mpsc::unbounded_channel::<(Event, Option<oneshot::Sender<()>>)>();
        tokio::spawn(async move {
            while let Some((e, ack)) = ev_rx.recv().await {
                // one event that panics must not stop the ones behind it (a `lost` is a call end)
                let fut = emit(e);
                guarded("event", fut).await;
                if let Some(ack) = ack {
                    let _ = ack.send(());
                }
            }
        });
        Arc::new(Manager {
            cfg,
            f,
            ev_tx,
            t: Mutex::new(Tables::default()),
        })
    }

    pub(super) fn t(&self) -> std::sync::MutexGuard<'_, Tables> {
        lock(&self.t)
    }

    pub(super) fn now(&self) -> u64 {
        match &self.cfg.now {
            Some(f) => f(),
            None => mono_now(),
        }
    }

    pub(super) fn emit(&self, e: Event) {
        let _ = self.ev_tx.send((e, None));
    }

    /// `emit`, and the returned receiver resolves once the event has been handed to the emit
    /// function and it has finished (R2-order, PARITY 360: Go's pump writes `first_frame` in its
    /// own goroutine BEFORE it forwards the frame, so the frame's `media_frame` can never overtake
    /// it; a task that must keep that order awaits this). It resolves too if the queue is gone.
    pub(super) fn emit_delivered(&self, e: Event) -> oneshot::Receiver<()> {
        let (tx, rx) = oneshot::channel();
        let _ = self.ev_tx.send((e, Some(tx)));
        rx
    }

    /// The number of live bridges.
    pub fn count(&self) -> usize {
        self.t().by_call.len()
    }

    /// Slots in use that are not yet published (a test seam: Go reads `m.pending`).
    #[cfg(feature = "test-seams")]
    pub fn pending_len(&self) -> usize {
        self.t().pending.len()
    }

    /// Entries left in every table (a test seam: Go sums the map lengths).
    #[cfg(feature = "test-seams")]
    pub fn table_entries(&self) -> usize {
        let g = self.t();
        g.by_call.len() + g.by_session.len() + g.by_leg.len() + g.owners.len() + g.pending.len()
    }

    /// The session id of a live call (a test seam: `bySessionOf`).
    #[cfg(feature = "test-seams")]
    pub fn session_of(&self, call_id: &str) -> Option<String> {
        self.t().by_call.get(call_id).map(|b| b.session.clone())
    }

    /// The counters of one leg of a live call: (rx, tx, dropped, bad, concealed, maxgap ns).
    #[cfg(feature = "test-seams")]
    pub fn counters(&self, call_id: &str, side: &str) -> Option<[i64; 6]> {
        let b = self.t().by_call.get(call_id).cloned()?;
        let ls = &b.legs[side_index(side)?];
        Some([
            ls.rx.load(Ordering::SeqCst),
            ls.tx.load(Ordering::SeqCst),
            ls.dropped.load(Ordering::SeqCst),
            ls.bad.load(Ordering::SeqCst),
            ls.concealed.load(Ordering::SeqCst),
            ls.max_gap.load(Ordering::SeqCst),
        ])
    }

    pub(super) fn validate(&self, call_id: &str, specs: &[LegSpec]) -> Result<(), HouseError> {
        let bad = |m: String| Err(HouseError::BadParams(m));
        if call_id.is_empty()
            || call_id.len() > MAX_CALL_ID_LEN
            || !call_id.starts_with(&self.cfg.call_prefix)
        {
            return bad(format!(
                "call_id must start with {}",
                crate::gostd::strconv::go_quote(&self.cfg.call_prefix)
            ));
        }
        if specs.len() != 2 {
            return bad("a house call has exactly two legs".to_string());
        }
        let mut seen: Vec<&str> = Vec::new();
        for s in specs {
            if s.side != SIDE_FROM && s.side != SIDE_TO {
                return bad(format!(
                    "side must be {} or {}",
                    crate::gostd::strconv::go_quote(SIDE_FROM),
                    crate::gostd::strconv::go_quote(SIDE_TO)
                ));
            }
            if seen.contains(&s.side.as_str()) {
                return bad(format!(
                    "side {} given twice",
                    crate::gostd::strconv::go_quote(&s.side)
                ));
            }
            seen.push(&s.side);
            if s.owner.len() > MAX_OWNER_LEN {
                return bad("owner is too long".to_string());
            }
            match s.kind.as_str() {
                KIND_FRAMES => {
                    if s.codec != "pcmu" && s.codec != "l16" {
                        return bad("frames leg codec must be pcmu or l16".to_string());
                    }
                }
                KIND_WEBRTC => {
                    if s.sdp.is_empty() {
                        return bad("webrtc leg needs the phone's offer".to_string());
                    }
                }
                _ => {
                    return bad(format!(
                        "kind must be {} or {}",
                        crate::gostd::strconv::go_quote(KIND_FRAMES),
                        crate::gostd::strconv::go_quote(KIND_WEBRTC)
                    ))
                }
            }
        }
        Ok(())
    }

    /// Under the table's lock: the checks of `Open` (shutdown, duplicate, the two caps), a fresh
    /// session id, the slot reserved (`pending`) and the owners counted. The legs are built
    /// afterwards, outside the lock.
    fn reserve(
        self: &Arc<Self>,
        call_id: &str,
        specs: &[LegSpec],
    ) -> Result<Arc<Bridge>, HouseError> {
        let rt = tokio::runtime::Handle::current();
        let mut g = self.t();
        let opening = g.pending.contains_key(call_id);
        if g.closed {
            return Err(HouseError::Shutdown);
        }
        if g.by_call.contains_key(call_id) || opening {
            return Err(HouseError::AlreadyOpen);
        }
        let n = g.by_call.len() + g.pending.len();
        if n >= self.cfg.max_bridges {
            return Err(HouseError::TooMany(format!("{n} bridges already open")));
        }
        let mut add: HashMap<&str, usize> = HashMap::new();
        for s in specs {
            if !s.owner.is_empty() {
                *add.entry(s.owner.as_str()).or_default() += 1;
            }
        }
        for (o, n) in &add {
            let held = g.owners.get(*o).copied().unwrap_or(0);
            if held + n > self.cfg.max_per_owner {
                return Err(HouseError::TooMany(format!(
                    "that phone already holds {held} legs"
                )));
            }
        }
        let new_id = || match &self.cfg.new_id {
            Some(f) => f(),
            None => random_hex(),
        };
        let mut session = format!("{SESSION_PREFIX}{}", new_id());
        while g.by_session.contains_key(&session) || g.pending.values().any(|s| *s == session) {
            session = format!("{SESSION_PREFIX}{}", new_id());
        }
        g.pending.insert(call_id.to_string(), session.clone());
        let mk = |side: &str| {
            let s = specs.iter().find(|s| s.side == side).expect("validated");
            Arc::new(LegState::new(
                side,
                &s.kind,
                &s.owner,
                format!("{session}-{side}"),
            ))
        };
        let legs = [mk(SIDE_FROM), mk(SIDE_TO)];
        for ls in &legs {
            if !ls.owner.is_empty() {
                *g.owners.entry(ls.owner.clone()).or_default() += 1;
            }
        }
        Ok(Arc::new(Bridge {
            m: self.clone(),
            call_id: call_id.to_string(),
            session,
            legs,
            started: AtomicU64::new(0),
            ended: AtomicBool::new(false),
            done: CancelToken::new(),
            released: AtomicBool::new(false),
            rt,
        }))
    }

    /// One leg from the factory: a frames leg, or a WebRTC leg whose offer is queued in `webrtc`.
    async fn build_leg(
        &self,
        leg_id: &str,
        idx: usize,
        s: &LegSpec,
        webrtc: &mut Vec<(usize, Arc<dyn WebRtcLeg>, String)>,
    ) -> Result<BuiltLeg, String> {
        match s.kind.as_str() {
            KIND_FRAMES => self.f.frames(leg_id, &s.codec).await.map(|l| {
                let leg: Arc<dyn HouseLeg> = l.clone();
                BuiltLeg {
                    input: leg.input(),
                    leg,
                    frames: Some(l),
                }
            }),
            _ => self.f.webrtc(leg_id).await.map(|l| {
                webrtc.push((idx, l.clone(), s.sdp.clone()));
                let leg: Arc<dyn HouseLeg> = l;
                BuiltLeg {
                    input: leg.input(),
                    leg,
                    frames: None,
                }
            }),
        }
    }

    /// `Open`: reserves a slot, builds both legs, starts the bridge and returns before any ICE
    /// gathering: a WebRTC leg's answer arrives later as an `sdp` event. Any failure leaves
    /// nothing behind, and so does dropping this future.
    ///
    /// The slot (cap, owner counts, the call id and the session id) is reserved first, under the
    /// lock, so concurrent opens cannot overshoot a cap; the legs are built and the bridge
    /// started before it is published in the lookup tables.
    pub async fn open(
        self: &Arc<Self>,
        call_id: &str,
        specs: Vec<LegSpec>,
    ) -> Result<Opened, HouseError> {
        self.validate(call_id, &specs)?;
        let b = self.reserve(call_id, &specs)?;
        let mut guard = OpenGuard {
            b: b.clone(),
            started: false,
            armed: true,
        };

        // Build the legs outside the lock (a factory may be slow).
        let mut opened = Opened {
            session: b.session.clone(),
            legs: Vec::new(),
        };
        let mut webrtc: Vec<(usize, Arc<dyn WebRtcLeg>, String)> = Vec::new();
        for s in &specs {
            let idx = side_index(&s.side).expect("validated");
            let ls = &b.legs[idx];
            let built = self.build_leg(&ls.leg_id, idx, s, &mut webrtc).await;
            match built {
                Ok(bl) => {
                    let _ = ls.built.set(bl);
                }
                Err(e) => {
                    guard.armed = false;
                    // on a task of its own: if this future is dropped here, the close of the
                    // leg built so far (whose work may be in the future it returns) still
                    // runs to the end (R2-104)
                    let b2 = b.clone();
                    let _ = b.rt.spawn(async move { b2.abort().await }).await;
                    return Err(HouseError::Failed(format!(
                        "house: building the {} leg: {e}",
                        s.side
                    )));
                }
            }
            let mut ol = OpenedLeg {
                side: s.side.clone(),
                kind: s.kind.clone(),
                leg_id: String::new(),
                codec: String::new(),
            };
            if s.kind == KIND_FRAMES {
                ol.leg_id = ls.leg_id.clone();
                ol.codec = s.codec.clone();
            }
            opened.legs.push(ol);
        }
        b.start();
        guard.started = true;
        if let Err(e) = self.publish(&b) {
            guard.armed = false;
            if let Some(t) = b.end("", REASON_SHUTDOWN, false) {
                t.run().await;
            }
            return Err(e);
        }
        guard.armed = false;
        for (idx, leg, offer) in webrtc {
            b.spawn_answer(idx, leg, offer);
        }
        Ok(opened)
    }

    /// Makes a started bridge reachable by `frame` / `close_leg` / `close` / `hold`. A bridge that
    /// already ended (a leg was gone at once; its slot is released) is not published and the open
    /// still stands; a manager that began stopping while the legs were built refuses.
    pub(super) fn publish(&self, b: &Arc<Bridge>) -> Result<(), HouseError> {
        let mut g = self.t();
        if b.released.load(Ordering::SeqCst) {
            return Ok(());
        }
        if g.closed {
            return Err(HouseError::Shutdown);
        }
        g.pending.remove(&b.call_id);
        g.by_call.insert(b.call_id.clone(), b.clone());
        g.by_session.insert(b.session.clone(), b.clone());
        g.by_leg.insert(b.legs[0].leg_id.clone(), (b.clone(), 0));
        g.by_leg.insert(b.legs[1].leg_id.clone(), (b.clone(), 1));
        Ok(())
    }

    /// `Frame`: one uplink frame to a frame-channel leg.
    pub fn frame(&self, leg_id: &str, payload: &[u8]) -> Result<(), FrameError> {
        let found = self.t().by_leg.get(leg_id).cloned();
        let Some((b, i)) = found else {
            return Err(FrameError::UnknownLeg);
        };
        let Some(frames) = b.legs[i].built.get().and_then(|bl| bl.frames.clone()) else {
            return Err(FrameError::UnknownLeg);
        };
        frames.push(payload).map_err(FrameError::Leg)
    }

    /// `CloseLeg`: a frame-channel leg's goodbye (the phone closed its channel): the call ends and
    /// the other leg is told.
    pub async fn close_leg(&self, leg_id: &str, reason: &str) -> bool {
        let found = self.t().by_leg.get(leg_id).cloned();
        let Some((b, i)) = found else {
            return false;
        };
        let reason = if reason.is_empty() {
            REASON_LOST
        } else {
            reason
        };
        if let Some(t) = b.end(b.legs[i].side, reason, true) {
            t.run().await;
        }
        true
    }

    /// `Close`: ends `call_id`'s bridge at Python's request (no events: Python asked).
    pub async fn close(&self, call_id: &str) -> bool {
        let b = self.t().by_call.get(call_id).cloned();
        let Some(b) = b else {
            return false;
        };
        if let Some(t) = b.end("", REASON_HANGUP, false) {
            t.run().await;
        }
        true
    }

    /// `Hold`: turns a leg's uplink into silence (and mutes its downlink), or releases it.
    pub fn hold(&self, call_id: &str, side: &str, on: bool) -> bool {
        let b = self.t().by_call.get(call_id).cloned();
        let Some(b) = b else {
            return false;
        };
        match side {
            SIDE_FROM => b.legs[0].held.store(on, Ordering::SeqCst),
            SIDE_TO => b.legs[1].held.store(on, Ordering::SeqCst),
            _ => return false,
        }
        true
    }

    /// `CloseAll`: ends every bridge (the hub is stopping) and refuses later opens. No events: the
    /// link is going away. Waits (bounded) for the WebRTC sessions to release their sockets.
    pub async fn close_all(&self) {
        let all: Vec<Arc<Bridge>> = {
            let mut g = self.t();
            g.closed = true;
            g.by_call.values().cloned().collect()
        };
        // All bridges at once, and the wait for them is bounded as a whole: a stalled Python (every
        // `media_closed` write waits out the 5 s write timeout) must not hold up the sessions and
        // drivers behind us. The tails that are still running at the bound go on in the background.
        let handles: Vec<_> = all
            .iter()
            .filter_map(|b| b.end("", REASON_SHUTDOWN, false))
            .map(|t| tokio::spawn(async move { t.run().await }))
            .collect();
        let _ = tokio::time::timeout(self.cfg.close_all_wait, async {
            for h in handles {
                let _ = h.await;
            }
        })
        .await;
        let waits: Vec<BoxFut> = all
            .iter()
            .flat_map(|b| b.legs.iter())
            .filter_map(|ls| ls.built.get().map(|bl| bl.leg.wait_released()))
            .collect();
        let _ = tokio::time::timeout(Duration::from_secs(2), async {
            for w in waits {
                w.await;
            }
        })
        .await;
    }

    /// `release`: drops a bridge from every table and returns its owners' slots, once.
    pub(super) fn release(&self, b: &Arc<Bridge>) {
        let mut g = self.t();
        if b.released.swap(true, Ordering::SeqCst) {
            return;
        }
        g.pending.remove(&b.call_id);
        if g.by_call.get(&b.call_id).is_some_and(|x| Arc::ptr_eq(x, b)) {
            g.by_call.remove(&b.call_id);
            g.by_session.remove(&b.session);
            g.by_leg.remove(&b.legs[0].leg_id);
            g.by_leg.remove(&b.legs[1].leg_id);
        }
        for ls in &b.legs {
            if !ls.owner.is_empty() {
                let n = g.owners.get_mut(&ls.owner).map(|n| {
                    *n = n.saturating_sub(1);
                    *n
                });
                if n == Some(0) {
                    g.owners.remove(&ls.owner);
                }
            }
        }
    }
}

/// Undoes a half-built or unpublished bridge when the `open` future is dropped (the request task
/// was cancelled): without it the slot, the owner counts and the built legs would leak.
pub(super) struct OpenGuard {
    pub(super) b: Arc<Bridge>,
    pub(super) started: bool,
    pub(super) armed: bool,
}

impl Drop for OpenGuard {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        let b = self.b.clone();
        if self.started {
            if let Some(t) = b.end("", REASON_SHUTDOWN, false) {
                b.rt.spawn(async move { t.run().await });
            }
        } else {
            let rt = b.rt.clone();
            rt.spawn(async move { b.abort().await });
        }
    }
}
