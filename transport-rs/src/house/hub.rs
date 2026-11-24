//! The hub's side: the legs over the real transports (`WsFramesLeg`, `SfuLeg`), the `HubFactory`, and the `HouseHub` that the dispatcher registers and the `media_*` router. The only file of this module that names `wsmedia::WsMediaHub` and `sfu::session`; with `params.rs` (the verb parameters) the only one that names `ipc`. The bridge core (`mod.rs`, `manager.rs`, `bridge.rs`) imports none of them (`tests/house_ipc.rs`).

use super::{
    BoxFut, Config, EmitFn, Event, Factory, FrameError, FramesLeg, HouseError, HouseLeg, Receiver,
    WebRtcLeg, KIND_FRAMES, SESSION_PREFIX, STATE_ATTACHED,
};
use crate::gojson;
use crate::util::base64_decode;
use crate::util::base64_encode;
use crate::util::PushError;
use std::panic::catch_unwind;
use std::panic::AssertUnwindSafe;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;

use serde_json::{json, Value};

use super::manager::Manager;
use super::params::decode_open_params;
use crate::gojson::{Kind, Spec};
use crate::ipc::{raw_str, HandlerResult, IpcError, Params};
use crate::wsmedia::{
    MediaNotifier, WsMediaHub, CODEC_PCMU, FRAME_MILLIS, L16_FRAME_BYTES, PCMU_FRAME_BYTES,
    WSMEDIA_PARAMS,
};

/// A [`crate::wsmedia::Leg`] as a house leg.
pub(super) struct WsFramesLeg(Arc<crate::wsmedia::Leg>);

impl HouseLeg for WsFramesLeg {
    fn input(&self) -> mpsc::Sender<Vec<u8>> {
        crate::call::ClientLeg::input(&*self.0)
    }
    fn output(&self) -> Receiver {
        crate::call::ClientLeg::output(&*self.0)
    }
    fn on_connected(&self, cb: Box<dyn FnOnce() + Send>) {
        crate::call::ClientLeg::on_connected(&*self.0, cb)
    }
    fn on_disconnected(&self, cb: Box<dyn FnOnce() + Send>) {
        crate::call::ClientLeg::on_disconnected(&*self.0, cb)
    }
    fn close(&self, reason: &str) -> BoxFut {
        let (l, r) = (self.0.clone(), reason.to_string());
        Box::pin(async move { l.close(&r).await })
    }
}

impl FramesLeg for WsFramesLeg {
    fn push(&self, payload: &[u8]) -> Result<(), PushError> {
        self.0.push(payload)
    }
}

/// A [`crate::sfu::session::Session`] as a house leg (`sfuHouseLeg`).
pub(super) struct SfuLeg(Arc<crate::sfu::session::Session>);

impl HouseLeg for SfuLeg {
    fn input(&self) -> mpsc::Sender<Vec<u8>> {
        self.0.audio_in()
    }
    fn output(&self) -> Receiver {
        self.0.audio_out()
    }
    fn on_connected(&self, cb: Box<dyn FnOnce() + Send>) {
        self.0.on_connected(cb)
    }
    fn on_disconnected(&self, cb: Box<dyn FnOnce() + Send>) {
        self.0.on_disconnected(cb)
    }
    fn close(&self, _reason: &str) -> BoxFut {
        // `Close(string)` drops the reason, as Go's `sfuHouseLeg` does.
        let s = self.0.clone();
        Box::pin(async move {
            if catch_unwind(AssertUnwindSafe(|| s.close())).is_err() {
                crate::panic_hook::err_line(
                    "portico-transport: house: closing a WebRTC leg panicked",
                );
            }
        })
    }
    fn wait_released(&self) -> BoxFut {
        let s = self.0.clone();
        Box::pin(async move { s.wait_done().await })
    }
}

impl WebRtcLeg for SfuLeg {
    fn handle_offer(&self, sdp: String) -> BoxFut<Result<String, String>> {
        let s = self.0.clone();
        Box::pin(async move { s.handle_offer(&sdp).await })
    }
}

/// `houseFactory`: builds the real legs.
pub(super) struct HubFactory {
    pub(super) notify: Arc<dyn MediaNotifier>,
    /// how long a frames leg may go without an uplink frame (`wsMediaIdle` in the hub)
    pub(super) idle: Duration,
    pub(super) sfu: crate::sfu::session::SessionConfig,
}

impl Factory for HubFactory {
    fn frames(&self, leg_id: &str, codec: &str) -> BoxFut<Result<Arc<dyn FramesLeg>, String>> {
        let (notify, idle, id, codec) = (
            self.notify.clone(),
            self.idle,
            leg_id.to_string(),
            codec.to_string(),
        );
        Box::pin(async move {
            // The hub's own idle rule (wsMediaIdle) is kept for a house leg: 5 s without an
            // uplink frame closes it.
            let (n, i) = (notify.clone(), id.clone());
            let send: crate::wsmedia::SendFn = Arc::new(move |payload: Vec<u8>| {
                n.notify(
                    "media_frame",
                    json!({"call_id": i, "d": base64_encode(&payload)}),
                )
            });
            let leg = crate::wsmedia::Leg::new(&codec, idle, send)?;
            let (n, i) = (notify, id);
            leg.on_closed(move |reason| {
                n.notify("media_closed", json!({"call_id": i, "reason": reason}))
            })
            .await;
            Ok(Arc::new(WsFramesLeg(leg)) as Arc<dyn FramesLeg>)
        })
    }

    fn webrtc(&self, leg_id: &str) -> BoxFut<Result<Arc<dyn WebRtcLeg>, String>> {
        let (cfg, id) = (self.sfu.clone(), leg_id.to_string());
        Box::pin(async move {
            let s = crate::sfu::session::Session::with_config(&id, cfg)?;
            Ok(Arc::new(SfuLeg(s)) as Arc<dyn WebRtcLeg>)
        })
    }
}

pub(super) const REASON_ONLY: [Spec; 1] = [Spec {
    json: "reason",
    kind: Kind::Str,
}];
pub(super) const CALL_ID_ONLY: [Spec; 1] = [Spec {
    json: "call_id",
    kind: Kind::Str,
}];

pub(super) fn bad_params(m: impl Into<String>) -> IpcError {
    IpcError::new("bad_params", m)
}

/// `houseHub`: the enabled side. The manager and the door-path handlers it routes around.
pub struct HouseHub {
    pub(super) mgr: Arc<Manager>,
    pub(super) door: Arc<WsMediaHub>,
}

impl HouseHub {
    /// `registerHouseWith`, enabled: builds the manager over the real legs. `emit` is the
    /// `house_leg_state` notifier.
    pub fn new(
        notify: Arc<dyn MediaNotifier>,
        door: Arc<WsMediaHub>,
        idle: Duration,
        sfu: crate::sfu::session::SessionConfig,
    ) -> Arc<HouseHub> {
        let n = notify.clone();
        let emit: EmitFn = Arc::new(move |e: Event| {
            let mut p = json!({"call_id": e.call_id, "leg": e.side, "state": e.state});
            if !e.reason.is_empty() {
                p["reason"] = Value::String(e.reason);
            }
            if !e.sdp.is_empty() {
                p["sdp"] = Value::String(e.sdp);
            }
            n.notify("house_leg_state", p)
        });
        let cfg = Config {
            logf: Some(Arc::new(|line: String| {
                crate::panic_hook::err_line(&format!("portico-transport: {line}"))
            })),
            ..Default::default()
        };
        let factory = Arc::new(HubFactory { notify, idle, sfu });
        Arc::new(HouseHub {
            mgr: Manager::new(cfg, factory, emit),
            door,
        })
    }

    /// A hub over a manager the caller built (the tests inject fake legs).
    pub fn with_manager(mgr: Arc<Manager>, door: Arc<WsMediaHub>) -> Arc<HouseHub> {
        Arc::new(HouseHub { mgr, door })
    }

    pub fn manager(&self) -> &Arc<Manager> {
        &self.mgr
    }

    /// `registerHouseWith` (enabled): the house verbs, and `media_frame` / `media_close`
    /// re-registered with the router around the door path's handlers.
    pub fn register(self: &Arc<Self>, srv: &crate::ipc::Server) {
        let key: crate::ipc::KeyFn = Arc::new(crate::dispatch::call_id_key);
        let h = self.clone();
        srv.handle_keyed("house_open", key.clone(), move |p: Params| {
            let h = h.clone();
            async move { h.open(p).await }
        });
        let h = self.clone();
        srv.handle_keyed("house_close", key, move |p: Params| {
            let h = h.clone();
            async move { h.close(p).await }
        });
        let h = self.clone();
        srv.handle_inline("media_frame", move |p: Params| {
            let h = h.clone();
            async move { h.route_frame(p).await }
        });
        let h = self.clone();
        srv.handle_inline("media_close", move |p: Params| {
            let h = h.clone();
            async move { h.route_close(p).await }
        });
    }

    /// The `stop` of `registerHouse`: ends every bridge.
    pub async fn stop(&self) {
        self.mgr.close_all().await;
    }

    /// `houseHub.open`.
    pub async fn open(&self, params: Params) -> HandlerResult {
        let p = decode_open_params(raw_str(&params)).map_err(bad_params)?;
        let res = match self.mgr.open(&p.call_id, p.legs).await {
            Ok(r) => r,
            Err(e @ HouseError::BadParams(_)) => return Err(bad_params(e.message())),
            Err(HouseError::AlreadyOpen) => return Err(IpcError::new("already_open", p.call_id)),
            Err(e @ HouseError::TooMany(_)) => {
                return Err(IpcError::new("too_many_calls", e.message()))
            }
            Err(HouseError::Shutdown) => {
                return Err(IpcError::new("shutting_down", "transport is stopping"))
            }
            Err(e) => return Err(IpcError::new("house_open_failed", e.message())),
        };
        let legs: Vec<Value> = res
            .legs
            .iter()
            .map(|l| {
                if l.kind == KIND_FRAMES {
                    let size = if l.codec == CODEC_PCMU { PCMU_FRAME_BYTES } else { L16_FRAME_BYTES };
                    json!({"side": l.side, "kind": l.kind, "state": STATE_ATTACHED,
                           "leg_id": l.leg_id, "codec": l.codec, "frame_bytes": size, "frame_ms": FRAME_MILLIS})
                } else {
                    // the answer follows as house_leg_state sdp, then attached when connected
                    json!({"side": l.side, "kind": l.kind, "state": "pending"})
                }
            })
            .collect();
        Ok(json!({"session": res.session, "legs": legs}).into())
    }

    /// `houseHub.close`.
    pub async fn close(&self, params: Params) -> HandlerResult {
        let id = gojson::decode(raw_str(&params), "houseCloseParams", &CALL_ID_ONLY)
            .map(|v| gojson::str_at(&v, 0))
            .unwrap_or_default();
        if id.is_empty() {
            return Err(bad_params("house_close needs call_id"));
        }
        Ok(json!({"ok": true, "closed": self.mgr.close(&id).await}).into())
    }

    /// `isHouseID`: the call id of a `media_*` request when it names a house leg.
    pub(super) fn house_id(params: &Params) -> Option<String> {
        // strict, as Go: `if json.Unmarshal(raw, &p) != nil { return "", false }`
        let v = gojson::decode(raw_str(params), "isHouseID", &CALL_ID_ONLY).ok()?;
        let id = gojson::str_at(&v, 0);
        id.starts_with(SESSION_PREFIX).then_some(id)
    }

    /// `routeFrame`: a house id here, every other id to the door path.
    pub async fn route_frame(&self, params: Params) -> HandlerResult {
        let Some(id) = Self::house_id(&params) else {
            return self.door.frame(params).await;
        };
        let v = gojson::decode(raw_str(&params), "wsMediaParams", &WSMEDIA_PARAMS)
            .map_err(bad_params)?;
        let data = gojson::str_at(&v, 2);
        // the same pre-decode cap as a door leg (DEV-796 C6)
        if data.len() > crate::wsmedia::MAX_FRAME_B64 {
            return Err(IpcError::new("bad_frame", PushError::BadFrame.message()));
        }
        let payload = base64_decode(&data).ok_or_else(|| bad_params("d is not base64"))?;
        match self.mgr.frame(&id, &payload) {
            Ok(()) => Ok(json!({"ok": true}).into()),
            Err(FrameError::Leg(PushError::BadFrame)) => {
                Err(IpcError::new("bad_frame", PushError::BadFrame.message()))
            }
            Err(_) => Err(IpcError::new("unknown_session", id)),
        }
    }

    /// `routeClose`: a phone's goodbye ends the call; closing an unknown leg is a no-op, as for
    /// a door.
    pub async fn route_close(&self, params: Params) -> HandlerResult {
        let Some(id) = Self::house_id(&params) else {
            return self.door.close(params).await;
        };
        // `_ = json.Unmarshal(raw, &p)`: a field of the wrong type is skipped, the others stay
        // (R1-99: `decode_lenient` keeps them, the error is ignored as Go ignores it)
        let reason = gojson::str_at(
            &gojson::decode_lenient(raw_str(&params), "wsMediaParams", &REASON_ONLY).0,
            0,
        );
        self.mgr.close_leg(&id, &reason).await;
        Ok(json!({"ok": true}).into())
    }
}
