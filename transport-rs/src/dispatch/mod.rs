//! The request dispatcher: the 18 requests `cmd/portico-transport/main.go` registers besides
//! `auth` (which is the TCP gate's, `ipc/tcp.rs`), the device registry, the shutdown sequence,
//! and the event sink that turns a driver's events into notifications (7 of the 10
//! notifications pass this sink; the rest are written by `webrtc`, `wsmedia` and `house`).
//! Every Go driver is ported (`gate_sip_server`, `gate_sip_observer`, `dahua_sip`,
//! `unifi_protect`, `unifi_protect_talk`); the `webrtc_*` handlers and the call sequencer live
//! in `webrtc.rs` and `call/`, `media_*` in `wsmedia.rs`, `house_*` in `house/`.

use crate::util::lock;
use crate::wsmedia::WS_MEDIA_IDLE;
use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tokio::sync::Notify;

use crate::cancel::CancelToken;
use crate::driver::{
    driver_error, CallReasonSink, CallStatsSink, Capabilities, CapabilitiesSink, Driver, ErrorReporter, EventSink,
    ListenStateSink, MediaTarget, Sentinel, STATE_ENDED,
};
use crate::evqueue::{Dropped, EvQueue};
use crate::gojson::{self, Kind, Spec};
use crate::ipc::{raw_str, HandlerResult, IpcError, KeyFn, Params, Reply, Server};
use crate::sfu::session::SessionConfig;
use crate::standalone::PreloadedSet;
use crate::webrtc::{RelayClosingSink, RelayRegistry, SessionRegistry};
use crate::wsmedia::WsMediaHub;

/// `notImplementedMessage` in main.go: keepalive, and hangup with no driver to end the call.
pub const NOT_IMPLEMENTED_MESSAGE: &str = "Phase 1: keepalive/hangup tracking not built yet (§9.5)";
/// `houseNotEnabledMessage` in house.go.
pub const HOUSE_NOT_ENABLED_MESSAGE: &str = "house calls are not enabled on this hub";

mod handlers;
mod params;
mod sink;

use params::*;
pub(crate) use params::{bad_params, WEBRTC_PARAMS};
pub use params::{call_id_key, configure_key, device_id_key};
use sink::*;
pub use sink::{IpcEventSink, Note};

/// The device registry and the handlers that use it (`deviceRegistry`, `configureHandler`,
/// `deconfigureHandler`, `dialHandler`, `hangupHandler`, ...).
pub struct Hub {
    drivers: Mutex<HashMap<String, Arc<dyn Driver>>>,
    events: Arc<IpcEventSink>,
    /// cancelled when the transport shuts down (`ctx` in main.go)
    ctx: CancelToken,
    /// the browser WebRTC sessions by call id (`sessionRegistry`)
    sessions: Arc<SessionRegistry>,
    /// the relay-mode device legs joined to a browser (`relayRegistry`)
    relays: Arc<RelayRegistry>,
    /// the `-host` flag: the address a relay's device-leg SDP advertises (`""`: the routed one)
    our_host: String,
    /// the house-call bridge when `-house-calls` is on (`stopHouse` in main.go's shutdown)
    house: Mutex<Option<Arc<crate::house::HouseHub>>>,
    /// the door's frame-leg table (read by the tests through `door_leg_ids`)
    ws: Mutex<Option<Arc<WsMediaHub>>>,
    /// the devices this process owns whatever the hub says (`-sip-from-env`, slice 10): the hub's
    /// `configure` of one adopts it, its `deconfigure` only detaches (`preloadedSet`)
    preloaded: Arc<PreloadedSet>,
    /// `shutdownOnce` (main.go's `sync.Once`): the first `shutdown` starts the sequence on a task
    /// of its own, every caller (the first included) waits for that task (R1-1, R2-108: the
    /// sequence must not die with the caller that happened to start it)
    shutdown_done: Mutex<Option<tokio::sync::watch::Receiver<bool>>>,
    /// drivers the tests register under a key of their own, tried before the real registry (a
    /// fake with gates and counters, configured through the real `configure` handler, R2-1)
    #[cfg(feature = "test-seams")]
    test_drivers: Mutex<HashMap<String, DriverMaker>>,
}

/// A constructor the tests give [`Hub::add_test_driver`].
#[cfg(feature = "test-seams")]
pub type DriverMaker = Arc<dyn Fn() -> Box<dyn Driver> + Send + Sync>;

impl Hub {
    fn new(
        srv: &Server,
        sfu: SessionConfig,
        our_host: &str,
        events: Arc<IpcEventSink>,
    ) -> Arc<Hub> {
        // R1-2: the events a driver emitted inside a handler go out before that handler's reply
        let weak = Arc::downgrade(&events);
        srv.set_reply_barrier(Arc::new(move || {
            let weak = weak.clone();
            Box::pin(async move {
                if let Some(ev) = weak.upgrade() {
                    ev.flush(Duration::from_secs(5)).await;
                }
            })
        }));
        Arc::new(Hub {
            drivers: Mutex::new(HashMap::new()),
            events,
            ctx: CancelToken::new(),
            sessions: SessionRegistry::new(sfu),
            relays: RelayRegistry::new(),
            our_host: our_host.to_string(),
            house: Mutex::new(None),
            ws: Mutex::new(None),
            preloaded: Arc::new(PreloadedSet::default()),
            shutdown_done: Mutex::new(None),
            #[cfg(feature = "test-seams")]
            test_drivers: Mutex::new(HashMap::new()),
        })
    }

    /// Whether `-house-calls` built a house bridge (a test seam: off, nothing exists).
    #[cfg(feature = "test-seams")]
    pub fn has_house(&self) -> bool {
        lock(&self.house).is_some()
    }

    /// The ids in the door's media table (a test seam: Go's tests read `hub.legs`, R2-77).
    #[cfg(feature = "test-seams")]
    pub fn door_leg_ids(&self) -> Vec<String> {
        lock(&self.ws).as_ref().map(|w| w.ids()).unwrap_or_default()
    }

    /// Makes `configure` accept `{"driver": key}` and build the driver with `make` (a test seam).
    #[cfg(feature = "test-seams")]
    pub fn add_test_driver(&self, key: &str, make: DriverMaker) {
        lock(&self.test_drivers).insert(key.to_string(), make);
    }

    pub fn sessions(&self) -> Arc<SessionRegistry> {
        self.sessions.clone()
    }

    pub(crate) fn relays(&self) -> &Arc<RelayRegistry> {
        &self.relays
    }

    pub(crate) fn our_host(&self) -> &str {
        &self.our_host
    }

    /// The relay registry (a test seam and the shutdown path).
    #[cfg(feature = "test-seams")]
    pub fn relay_registry(&self) -> Arc<RelayRegistry> {
        self.relays.clone()
    }

    pub(crate) fn ctx_token(&self) -> CancelToken {
        self.ctx.clone()
    }

    /// A hub with no IPC connection, for tests: notifications stay in `events`.
    #[cfg(test)]
    fn detached() -> Arc<Hub> {
        Arc::new(Hub {
            drivers: Mutex::new(HashMap::new()),
            events: IpcEventSink::detached(),
            ctx: CancelToken::new(),
            sessions: SessionRegistry::new(SessionConfig::loopback()),
            relays: RelayRegistry::new(),
            our_host: String::new(),
            house: Mutex::new(None),
            ws: Mutex::new(None),
            preloaded: Arc::new(PreloadedSet::default()),
            shutdown_done: Mutex::new(None),
            #[cfg(feature = "test-seams")]
            test_drivers: Mutex::new(HashMap::new()),
        })
    }

    fn drivers(&self) -> std::sync::MutexGuard<'_, HashMap<String, Arc<dyn Driver>>> {
        lock(&self.drivers)
    }

    fn get(&self, id: &str) -> Option<Arc<dyn Driver>> {
        self.drivers().get(id).cloned()
    }

    /// Stores `d` under `id` unless the id is taken; reports whether it did.
    fn put_if_absent(&self, id: &str, d: Arc<dyn Driver>) -> bool {
        let mut g = self.drivers();
        if g.contains_key(id) {
            return false;
        }
        g.insert(id.to_string(), d);
        true
    }

    fn remove(&self, id: &str) -> Option<Arc<dyn Driver>> {
        self.drivers().remove(id)
    }

    /// Every configured driver, ordered by device id (Go iterates a map in random order).
    pub(crate) fn all(&self) -> Vec<Arc<dyn Driver>> {
        let g = self.drivers();
        let mut v: Vec<_> = g.iter().collect();
        v.sort_by(|a, b| a.0.cmp(b.0));
        v.into_iter().map(|(_, d)| d.clone()).collect()
    }

    /// `shutdown` of main.go: cancels the context and stops every driver, waiting for each
    /// (the clean de-register and the BYE of a live call go out before the process exits).
    ///
    /// Single-flight, as Go's `sync.Once` (R1-1): the sequence runs once, on a task of its own,
    /// and every caller waits until it has stopped every driver, so the process never exits while
    /// a de-REGISTER or the BYE of a live call is still going out, even if the caller that
    /// started it is dropped half-way (R2-108).
    pub async fn shutdown(self: &Arc<Hub>) {
        let mut done = lock(&self.shutdown_done)
            .get_or_insert_with(|| {
                let (tx, rx) = tokio::sync::watch::channel(false);
                let me = self.clone();
                tokio::spawn(async move {
                    me.shutdown_inner().await;
                    let _ = tx.send(true);
                });
                rx
            })
            .clone();
        let _ = done.wait_for(|d| *d).await;
    }

    async fn shutdown_inner(&self) {
        self.ctx.cancel();
        // stopHouse(): right after cancel(), before the drivers stop
        let house = lock(&self.house).clone();
        if let Some(h) = house {
            h.stop().await;
        }
        self.sessions.close_all_wait(Duration::from_secs(2)).await;
        // a relay whose browser session was closed above is already closing (its disconnect
        // callback took it out of the registry); this reaches the ones that are still joined
        self.relays.close_all(Duration::from_secs(3)).await;
        let all: Vec<Arc<dyn Driver>> = {
            let mut g = self.drivers();
            g.drain().map(|(_, d)| d).collect()
        };
        // all at once: one unreachable device's de-register must not delay the others
        let mut set = tokio::task::JoinSet::new();
        for d in all {
            set.spawn(async move {
                let _ = d.stop().await;
            });
        }
        while set.join_next().await.is_some() {}
        // what the drivers emitted while stopping (the `ended` of a live call) reaches the hub
        // before the socket is closed (R1-16)
        self.events.flush(Duration::from_secs(2)).await;
    }

    /// `adoptConfigure` around `configure` (see [`crate::standalone::adopt_configure`]).
    async fn configure_or_adopt(&self, params: Params) -> HandlerResult {
        crate::standalone::adopt_configure(
            &self.preloaded,
            |id| self.get(id).map(|d| d.capabilities()),
            params,
            |p| self.configure(p),
        )
        .await
    }

    /// `detachDeconfigure` around `deconfigure`.
    async fn deconfigure_or_detach(&self, params: Params) -> HandlerResult {
        crate::standalone::detach_deconfigure(&self.preloaded, params, |p| self.deconfigure(p))
            .await
    }

    /// `preloadSIP`: starts the env-configured `gate_sip_server` through the real `configure`.
    pub async fn preload_sip(&self, cfg: serde_json::Map<String, Value>) -> Result<(), String> {
        crate::standalone::preload_sip(|p| self.configure(p), &self.preloaded, cfg).await
    }

    /// The preloaded set (a test seam).
    #[cfg(feature = "test-seams")]
    pub fn preloaded(&self) -> Arc<PreloadedSet> {
        self.preloaded.clone()
    }

    /// The configured driver for `id` (a test seam).
    #[cfg(feature = "test-seams")]
    pub fn driver(&self, id: &str) -> Option<Arc<dyn Driver>> {
        self.get(id)
    }
}

/// What `register` takes besides the server.
#[derive(Clone)]
pub struct HubOptions {
    /// the `-house-calls` flag
    pub house_calls: bool,
    /// the WebRTC session config (the tests pin the interfaces)
    pub sfu: SessionConfig,
    /// the `-host` flag: the IPv4 address a relay-mode device leg advertises in its SDP (`""`:
    /// the address of the SIP socket that talks to the device)
    pub our_host: String,
}

impl Default for HubOptions {
    fn default() -> HubOptions {
        HubOptions {
            house_calls: false,
            sfu: SessionConfig::from_process_env(),
            our_host: String::new(),
        }
    }
}

/// One handler that calls `$obj.$method(params)` (the same four lines for each of the hub's
/// methods and the three `media_*` ones).
macro_rules! route {
    ($obj:expr, $method:ident) => {{
        let h = $obj.clone();
        move |p: Params| {
            let h = h.clone();
            async move { h.$method(p).await }
        }
    }};
}

/// Registers every method on `srv`, in the same dispatch classes as main.go. Returns the hub;
/// the caller shuts it down when the transport stops.
pub fn register(srv: &Server, o: HubOptions) -> Arc<Hub> {
    register_on(srv, o, IpcEventSink::new(srv.clone()))
}

/// `register` with the event sink given (the tests slow its writer down).
fn register_on(srv: &Server, o: HubOptions, events: Arc<IpcEventSink>) -> Arc<Hub> {
    let HubOptions {
        house_calls,
        sfu,
        our_host,
    } = o;
    let our_host = our_host.as_str();
    let keyed = |f: fn(&str) -> String| -> KeyFn { Arc::new(f) };
    let sfu_for_house = sfu.clone();
    let hub = Hub::new(srv, sfu, our_host, events);

    // configure / deconfigure: keyed per device_id.
    srv.handle_keyed(
        "configure",
        keyed(configure_key),
        route!(hub, configure_or_adopt),
    );
    srv.handle_keyed(
        "deconfigure",
        keyed(device_id_key),
        route!(hub, deconfigure_or_detach),
    );

    srv.handle("dial", route!(hub, dial));
    srv.handle_keyed(
        "external_ring",
        keyed(device_id_key),
        route!(hub, external_ring),
    );
    srv.handle("talk_health", route!(hub, talk_health));
    srv.handle("answer", route!(hub, answer));
    srv.handle("open_relay", route!(hub, open_relay));

    // webrtc_*: keyed per call_id.
    srv.handle_keyed(
        "webrtc_offer",
        keyed(call_id_key),
        route!(hub, webrtc_offer),
    );
    srv.handle_keyed(
        "webrtc_answer",
        keyed(call_id_key),
        route!(hub, webrtc_answer),
    );
    srv.handle_keyed("webrtc_ice", keyed(call_id_key), route!(hub, webrtc_ice));

    // media_*: the inline audio lane.
    let join_hub = hub.clone();
    let join: crate::wsmedia::JoinFn = Arc::new(move |call_id, leg| {
        // `joinCallToBrowser(ctx, registry, relays, *ourHost, callID, leg)`
        crate::webrtc::join_call_to_browser_with(
            &join_hub.ctx_token(),
            &join_hub.all(),
            join_hub.relays(),
            join_hub.our_host(),
            call_id,
            leg,
        );
    });
    let ws = WsMediaHub::new(Arc::new(srv.clone()), WS_MEDIA_IDLE, join);
    let ws_door = ws.clone();
    *lock(&hub.ws) = Some(ws.clone());
    let house_sfu = sfu_for_house;
    srv.handle_inline("media_open", route!(ws, open));
    srv.handle_inline("media_frame", route!(ws, frame));
    srv.handle_inline("media_close", route!(ws, close));

    // house_open / house_close (registerHouse).
    if house_calls {
        let house_hub =
            crate::house::HouseHub::new(Arc::new(srv.clone()), ws_door, WS_MEDIA_IDLE, house_sfu);
        *lock(&hub.house) = Some(house_hub.clone());
        house_hub.register(srv);
    } else {
        for method in ["house_open", "house_close"] {
            srv.handle(method, |_p: Params| async move {
                Err(IpcError::new("not_supported", HOUSE_NOT_ENABLED_MESSAGE))
            });
        }
    }

    srv.handle("stream_url", route!(hub, stream_url));
    srv.handle("keepalive", |_p: Params| async move {
        Err(IpcError::new("not_implemented", NOT_IMPLEMENTED_MESSAGE))
    });
    srv.handle("hangup", route!(hub, hangup));
    hub
}

#[cfg(test)]
mod tests;
