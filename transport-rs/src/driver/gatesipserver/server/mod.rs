//! `server.go`: the SIP registrar + UAS the station registers to (E1), its RTP loops and the
//! event queue. One [`Server`] owns the two UDP sockets, the single call slot (`cur`), the one
//! audio client, the stats and the timers; the driver glue in `mod.rs` owns a `Server`.
//!
//! The call WE place to the station (`dial.go`) is the child module `dial`
//! (`dial.rs`): it reads the server's private state, as Go's `dial.go` shares the package.
//!
//! # Concurrency (see DESIGN.md)
//!
//! * Go's `Server.mu` is one `std::sync::Mutex<State>`, held for map/field work only and never
//!   across an `.await` (nothing in this file awaits while it is held). A poisoned lock is
//!   recovered, not propagated: a panic in one handler must not wedge the server.
//! * Sends are synchronous (`std::net::UdpSocket` in non-blocking mode, a `dup` of the socket the
//!   tokio side reads from), so the 100/180 pair, the 200 and a BYE go out in call order and the
//!   public methods (`attach`, `hangup`, `AudioClient::goodbye`) are plain functions like Go's.
//!   A full send buffer drops the datagram (Go would block); UDP is lossy and the station
//!   retransmits.
//! * One task each for the SIP read loop, the RTP read loop and the event dispatcher; one RTP
//!   sender per answered call. Timers (ring limit, orphan grace, registration lapse) are tasks
//!   that select on a [`CancelToken`], so stopping a timer that has already fired does nothing
//!   (Go's `Timer.Stop` has the same property) and a timer is never left behind by an ended call.
//! * The read loops contain handler panics (`catch_unwind`) and survive transient socket errors.
//! * The event queue (`evqueue.rs`) is bounded and evicts the oldest low-value event first; it never
//!   drops a ring or the end of a call before [`EVENT_QUEUE_HARD`] (PARITY 33 and 47). Go's is an
//!   unbounded slice.

use crate::util::lock;
use std::collections::HashMap;
use std::net::{IpAddr, Ipv6Addr, SocketAddr};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::{Arc, Mutex, Weak};
use std::time::{Duration, Instant};

use tokio::net::UdpSocket;
use tokio::sync::Notify;
use tokio::task::JoinHandle;

mod client;
mod dial;
mod events;
mod registrar;
mod rtp;
mod uas;

#[cfg(feature = "test-seams")]
pub use dial::DialTimers;
pub use dial::{DialError, OutCall};
pub use rtp::{rtp_source_ok, FarRtp};

use events::*;
use registrar::*;
use uas::*;

use super::audio::{AudioClient, AudioStats, ClientHost};
use super::config::Config;
use super::media::{
    alaw_decode, alaw_encode, choose_media, find_c_ip, find_m_port, media_wide, sdp_for, Media,
};
use super::netaddr::{parse_addr, resolve_udp_addr};
use super::{GateSipServer, ServerApi, ServerError};
use crate::cancel::CancelToken;
use crate::driver::{
    BoxFuture, Capabilities, EventSink, STATE_ACTIVE, STATE_ANSWERED, STATE_ENDED,
};
use crate::evqueue::{Dropped, EvQueue};
use crate::gostd::net::join_host_port;
use crate::gostd::parse::duration_string_of;
use crate::gostd::strconv::go_quote;
use crate::gostd::text::go_lossy;
use crate::sip::digest::{authorization_nonce_raw, verify_digest_authorization_raw};
use crate::sip::{
    parse_request, parse_rtp_packet, DigestResult, NonceStore, ParsedRequest, RtpPacket,
};

/// The most events waiting for the dispatcher. Go's queue is unbounded; a flood of
/// INVITE/CANCEL pairs from the network must not grow memory without limit.
pub const EVENT_QUEUE_MAX: usize = 1024;
/// Rings and call ends are never evicted; they only stop being queued at this many.
pub const EVENT_QUEUE_HARD: usize = 4096;
/// `regExpires + 30s` (dial.go `defaultRegTTL`): a registration older than this is not trusted.
pub const DEFAULT_REG_TTL: Duration = Duration::from_secs(330);

/// `Stats`, the analogue of gate-sip's /health, for tests and the driver.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Stats {
    /// set on a good REGISTER (digest pass)
    pub registered: bool,
    pub registered_from: String,
    pub digest_ok: i64,
    pub digest_mismatch: i64,
    pub calls: i64,
    pub answered: i64,
    pub rtp_in: i64,
    pub rtp_out: i64,
    pub rtp_rejected: i64,
    pub last_err: String,
    pub ringing: bool,
    pub in_call: bool,
    pub dialog_confirmed: bool,
    pub app_attached: bool,
    pub frames_to_app: i64,
    pub frames_from_app: i64,
    pub dropped_up: i64,
    pub dropped_down: i64,
    /// datagrams refused by `allow_from`
    pub dropped: i64,
    /// INVITEs placed to the station (dial.go)
    pub dials: i64,
    /// an outbound call is ringing out (dial.go)
    pub dialing: bool,
    /// Rust only: INVITEs answered although their SDP media port is not a UDP port (above
    /// 65535, or too many digits), so no RTP can be sent for them (Go counts nothing)
    pub unsendable_media: i64,
}

/// Go's `call`. `out` is set for a call WE placed (dial.go), `None` for an inbound one.
pub struct Call {
    /// identity of this call object (Go compares the pointers)
    serial: u64,
    h: Hdrs,
    /// the station's SIP source
    addr: SocketAddr,
    far_rtp: Option<FarRtp>,
    tag: String,
    /// our address as seen from the station (never the bind wildcard)
    local_ip: String,
    media: Media,
    answered: bool,
    confirm: bool,
    ring_timer: Option<CancelToken>,
    orphan_timer: Option<CancelToken>,
    /// cancelled when the call is over: stops the RTP sender
    stop: CancelToken,
    // Per-call RTP counters, for the summary line at the call's end and the one-per-call
    // rejection log (DEV-518 D3).
    rtp_in: i64,
    rtp_out: i64,
    rtp_rejected: i64,
    reject_logged: bool,
    /// a BYE/CANCEL from another host was ignored and logged (once per call)
    end_ignored_logged: bool,
    /// non-`None`: a call WE placed to the station (dial.go); `None`: inbound
    out: Option<Arc<OutCall>>,
}

impl Call {
    fn id(&self) -> String {
        go_lossy(&self.h.call_id)
    }

    /// Whether this is the dialog of `call_id` (compared as bytes, as Go compares strings).
    fn is(&self, call_id: &[u8]) -> bool {
        self.h.call_id == call_id
    }

    /// A new call: not answered, no timers, no counts, wideband media until negotiated (R1-174:
    /// the three places that build a call set only what differs).
    fn new(serial: u64, h: Hdrs, addr: SocketAddr, tag: String, local_ip: String) -> Call {
        Call {
            serial,
            h,
            addr,
            far_rtp: None,
            tag,
            local_ip,
            media: media_wide(),
            answered: false,
            confirm: false,
            ring_timer: None,
            orphan_timer: None,
            stop: CancelToken::new(),
            rtp_in: 0,
            rtp_out: 0,
            rtp_rejected: 0,
            reject_logged: false,
            end_ignored_logged: false,
            out: None,
        }
    }

    /// A call that has only a media address, for the RTP source rule's test.
    #[cfg(test)]
    pub(crate) fn bare(far_rtp: Option<FarRtp>) -> Call {
        Call {
            far_rtp,
            ..Call::new(
                0,
                Hdrs::default(),
                SocketAddr::from(([0, 0, 0, 0], 0)),
                String::new(),
                String::new(),
            )
        }
    }
}

struct State {
    cur: Option<Call>,
    /// the one audio client, and the serial of the call it belongs to (`cl.call`)
    client: Option<(Arc<AudioClient>, u64)>,
    st: Stats,
    closed: bool,
    reg: Option<Registration>,
    reg_timer: Option<CancelToken>,
    /// what the last published capabilities said: (CanDial, StationRegistered, LastRegisterAt)
    caps_pub: (bool, bool, u64),
    /// unix seconds of the last good REGISTER (0 = none); survives a de-register (DEV-518)
    reg_unix: u64,
    reg_ttl: Duration,
    /// dial.go: `dialRing`, `dialCap`, `dialAtt`, `cancelW`
    dial_ring: Duration,
    dial_cap: Duration,
    dial_att: Duration,
    cancel_w: Duration,
    last_drop_log: HashMap<IpAddr, Instant>,
    next_serial: u64,
    /// Test seam: INVITEs are accepted from any allowed host (the pre-DEV-796-S1 behaviour), so
    /// the Go-ported tests that never REGISTER keep their shape.
    #[cfg(feature = "test-seams")]
    open_invites: bool,
}

type Logger = Arc<dyn Fn(&str) + Send + Sync>;

/// The pause between two retries of a failing read loop.
const READ_RETRY_PAUSE: Duration = Duration::from_millis(10);

/// The retry policy of the SIP and RTP read loops (PARITY 35/330): Go returns on its first read
/// error, the port keeps reading and gives up only after more than 100 errors IN A ROW; a good
/// read resets the count.
#[derive(Default)]
pub(super) struct ReadErrors(u32);

impl ReadErrors {
    pub(super) fn ok(&mut self) {
        self.0 = 0;
    }

    /// Counts one failed read; true when the loop should give up.
    pub(super) fn failed(&mut self) -> bool {
        self.0 += 1;
        self.0 > 100
    }
}

/// `crypto/rand` letters and digits (`tok`).
fn tok(n: usize) -> String {
    const ALPHABET: &[u8] = b"abcdefghijklmnopqrstuvwxyz0123456789";
    let mut b = vec![0u8; n];
    crate::util::fill_random(&mut b);
    b.iter()
        .map(|&x| char::from(ALPHABET[usize::from(x) % ALPHABET.len()]))
        .collect()
}

/// `log.Printf`'s default line: the crate-wide stamped log line.
fn default_log(line: &str) {
    crate::panic_hook::log_line(line);
}

/// `localIPToward`: the address THIS host uses to reach `addr`. A UDP "connect" sends nothing;
/// it only asks the routing table.
fn local_ip_toward(addr: SocketAddr) -> String {
    let bind = if addr.is_ipv4() {
        "0.0.0.0:0"
    } else {
        "[::]:0"
    };
    let Ok(s) = std::net::UdpSocket::bind(bind) else {
        return String::new();
    };
    if s.connect(addr).is_err() {
        return String::new();
    }
    s.local_addr()
        .map(|a| a.ip().to_string())
        .unwrap_or_default()
}

/// The socket pair of a bound address: the tokio socket the read loop owns and a `dup` of it
/// for synchronous sends (both share the non-blocking file description).
fn bind_udp(addr: SocketAddr) -> std::io::Result<(UdpSocket, Arc<std::net::UdpSocket>)> {
    // Go listens dual-stack on a wildcard; do not leave IPV6_V6ONLY to the system default.
    let raw = socket2::Socket::new(
        socket2::Domain::for_address(addr),
        socket2::Type::DGRAM,
        Some(socket2::Protocol::UDP),
    )?;
    if addr.is_ipv6() {
        raw.set_only_v6(false)?;
    }
    raw.bind(&addr.into())?;
    let s: std::net::UdpSocket = raw.into();
    s.set_nonblocking(true)?;
    let send = s.try_clone()?;
    Ok((UdpSocket::from_std(s)?, Arc::new(send)))
}

/// Go prints the bound address of a nil-IP listen as `:port`.
fn go_listen_text(empty_host: bool, a: SocketAddr) -> String {
    if empty_host {
        format!(":{}", a.port())
    } else {
        a.to_string()
    }
}

/// Go's `net` error text for a failed `ListenUDP`.
///
/// `addr` is Go's text of the address: an empty host (`:5060`) prints without one.
fn listen_err(addr: &str, e: &std::io::Error) -> String {
    let why = match e.kind() {
        std::io::ErrorKind::AddrInUse => "address already in use".to_string(),
        std::io::ErrorKind::PermissionDenied => "permission denied".to_string(),
        std::io::ErrorKind::AddrNotAvailable => "cannot assign requested address".to_string(),
        _ => crate::gojson::io_err(e),
    };
    format!("listen udp {addr}: bind: {why}")
}

/// Go's `*net.UDPAddr` for an address the socket handed back (IPv4-mapped IPv6 unmapped).
/// An IPv6 address keeps its scope id (Go's `UDPAddr.Zone`): without it a reply to a link-local
/// station fails with EINVAL.
fn canon(a: SocketAddr) -> SocketAddr {
    match a {
        SocketAddr::V6(v6) => match v6.ip().to_ipv4_mapped() {
            Some(v4) => SocketAddr::new(IpAddr::V4(v4), v6.port()),
            None => a,
        },
        v4 => v4,
    }
}

/// The destination form a socket of the given family accepts: an IPv4 peer goes to an IPv6
/// (dual-stack) socket as an IPv4-mapped address.
fn dest_for(sock_is_v6: bool, addr: SocketAddr) -> SocketAddr {
    match addr {
        SocketAddr::V4(v4) if sock_is_v6 => {
            SocketAddr::new(IpAddr::V6(v4.ip().to_ipv6_mapped()), v4.port())
        }
        d => d,
    }
}

/// The SIP server and its RTP socket.
pub struct Server {
    cfg: Config,
    events: Arc<dyn EventSink>,
    logger: Mutex<Logger>,

    sip_send: Mutex<Option<Arc<std::net::UdpSocket>>>,
    rtp_send: Mutex<Option<Arc<std::net::UdpSocket>>>,
    sip_local: SocketAddr,
    sip_port: u16,
    rtp_port: u16,
    rtp_v6: bool,

    state: Mutex<State>,
    audio_stats: Arc<AudioStats>,
    nonces: NonceStore,

    evq: EvQueue<Event>,
    evsig: Notify,

    stop: CancelToken,
    /// cancelled when `shutdown` has finished (a second caller waits for it)
    done: CancelToken,
    tasks: Mutex<Vec<JoinHandle<()>>>,
    senders: Mutex<Vec<JoinHandle<()>>>,
    me: Weak<Server>,
}

impl Server {
    /// `newServer(cfg, events).start()`: binds the SIP and RTP sockets and starts the loops.
    /// The RTP socket binds the same host as SIP: a loopback-configured mode must not open a
    /// wildcard media port.
    pub async fn start(cfg: Config, events: Arc<dyn EventSink>) -> Result<Arc<Server>, String> {
        let listen = cfg.sip_listen.clone();
        let resolved = {
            let l = listen.clone();
            tokio::task::spawn_blocking(move || resolve_udp_addr(&l))
                .await
                .map_err(|_| "internal error".to_string())
        };
        let sa = resolved
            .and_then(|r| r)
            .map_err(|e| format!("gate_sip_server: bad sip_listen {}: {e}", go_quote(&listen)))?;
        let ip: IpAddr = match &sa.ip {
            Some(a) => a.to_ip(),
            None => IpAddr::V6(Ipv6Addr::UNSPECIFIED),
        };
        let sip_bind = SocketAddr::new(ip, sa.port);
        let (sip_rx, sip_tx) = bind_udp(sip_bind).map_err(|e| {
            format!(
                "gate_sip_server: cannot listen on {listen}: {}",
                listen_err(&go_listen_text(sa.ip.is_none(), sip_bind), &e)
            )
        })?;
        let rtp_bind = SocketAddr::new(ip, u16::try_from(cfg.rtp_port).unwrap_or(0));
        let (rtp_rx, rtp_tx) = match bind_udp(rtp_bind) {
            Ok(p) => p,
            Err(e) => {
                let host = sa
                    .ip
                    .as_ref()
                    .map_or("<nil>".to_string(), |a| a.to_ip().to_string());
                return Err(format!(
                    "gate_sip_server: cannot listen for RTP on {host}:{}: {}",
                    cfg.rtp_port,
                    listen_err(&go_listen_text(sa.ip.is_none(), rtp_bind), &e)
                ));
            }
        };
        let sip_local = sip_rx.local_addr().map_err(|e| e.to_string())?;
        let rtp_local = rtp_rx.local_addr().map_err(|e| e.to_string())?;

        let ring = duration_string_of(cfg.ring_limit);
        let orphan = duration_string_of(cfg.orphan_grace);
        let line = format!(
            "gate_sip_server: SIP on {sip_local}, RTP on :{}, realm {}, user {}, ring limit {ring}, orphan grace {orphan}",
            rtp_local.port(),
            go_quote(&cfg.realm),
            go_quote(&cfg.user)
        );
        let srv = Arc::new_cyclic(|me| Server {
            cfg,
            events,
            logger: Mutex::new(Arc::new(default_log)),
            sip_send: Mutex::new(Some(sip_tx)),
            rtp_send: Mutex::new(Some(rtp_tx)),
            sip_local,
            sip_port: sip_local.port(),
            rtp_port: rtp_local.port(),
            rtp_v6: rtp_local.is_ipv6(),
            state: Mutex::new(State {
                cur: None,
                client: None,
                st: Stats::default(),
                closed: false,
                reg: None,
                reg_timer: None,
                caps_pub: (false, false, 0),
                reg_unix: 0,
                reg_ttl: DEFAULT_REG_TTL,
                dial_ring: dial::DEFAULT_DIAL_RING,
                dial_cap: dial::DEFAULT_DIAL_CAP,
                dial_att: dial::DEFAULT_DIAL_ATTACH,
                cancel_w: dial::DEFAULT_CANCEL_WAIT,
                last_drop_log: HashMap::new(),
                next_serial: 1,
                #[cfg(feature = "test-seams")]
                open_invites: false,
            }),
            audio_stats: Arc::new(AudioStats::default()),
            nonces: NonceStore::new(),
            evq: EvQueue::new(EVENT_QUEUE_MAX, EVENT_QUEUE_HARD, Event::is_critical),
            evsig: Notify::new(),
            stop: CancelToken::new(),
            done: CancelToken::new(),
            tasks: Mutex::new(Vec::new()),
            senders: Mutex::new(Vec::new()),
            me: me.clone(),
        });
        let mut tasks = lock(&srv.tasks);
        tasks.push(tokio::spawn(srv.clone().sip_loop(sip_rx)));
        tasks.push(tokio::spawn(srv.clone().rtp_loop(rtp_rx)));
        tasks.push(tokio::spawn(srv.clone().dispatch_loop()));
        drop(tasks);
        srv.logf(line);
        Ok(srv)
    }

    fn arc(&self) -> Arc<Server> {
        self.me
            .upgrade()
            .expect("the server is alive while a method runs")
    }

    /// `SIPAddr`: what was actually bound (tests bind port 0).
    pub fn sip_addr(&self) -> SocketAddr {
        self.sip_local
    }

    /// `RTPPort`.
    pub fn rtp_port(&self) -> u16 {
        self.rtp_port
    }

    /// The device id this server rings under (`cfg.deviceID`).
    pub fn device_id(&self) -> &str {
        &self.cfg.device_id
    }

    /// Replaces the log sink (Go's tests overwrite `s.logf`). The callback must not call back
    /// into the server: it can run with the state lock held.
    pub fn set_logger(&self, f: impl Fn(&str) + Send + Sync + 'static) {
        *lock(&self.logger) = Arc::new(f);
    }

    /// Test seam: moves the nonce store's clock forward.
    #[cfg(feature = "test-seams")]
    pub fn advance_nonce_clock(&self, d: Duration) {
        self.nonces.advance(d);
    }

    /// Test seam (dial_test.go sets `s.regTTL`).
    #[cfg(feature = "test-seams")]
    pub fn set_reg_ttl(&self, d: Duration) {
        lock(&self.state).reg_ttl = d;
    }

    /// Test seam: accept an INVITE from any allowed host again (before DEV-796 S1, PARITY.md 473).
    #[cfg(feature = "test-seams")]
    pub fn set_open_invites(&self, open: bool) {
        lock(&self.state).open_invites = open;
    }

    /// Test seam (dial_test.go writes `s.reg.at`): makes the last registration `by` older.
    #[cfg(feature = "test-seams")]
    pub fn backdate_registration(&self, by: Duration) {
        if let Some(r) = lock(&self.state).reg.as_mut() {
            r.at = Instant::now().checked_sub(by).unwrap_or(r.at);
        }
    }

    /// Test seam: the IP the live call's RTP is sent to (Go's tests read `s.cur.farRTP`).
    #[cfg(feature = "test-seams")]
    pub fn far_rtp(&self) -> Option<FarRtp> {
        lock(&self.state).cur.as_ref().and_then(|c| c.far_rtp)
    }

    /// Resolves when the server has been shut down.
    pub async fn stopped(&self) {
        self.stop.cancelled().await;
    }

    fn logf(&self, line: String) {
        let f = lock(&self.logger).clone();
        f(&line);
    }

    // ── SIP ───────────────────────────────────────────────────────────────────────────────────

    async fn sip_loop(self: Arc<Self>, sock: UdpSocket) {
        let mut buf = vec![0u8; 65535];
        let mut errors = ReadErrors::default();
        loop {
            let r = tokio::select! {
                biased;
                _ = self.stop.cancelled() => return,
                r = sock.recv_from(&mut buf) => r,
            };
            match r {
                Ok((n, from)) => {
                    errors.ok();
                    let raw = buf[..n].to_vec();
                    let from = canon(from);
                    if catch_unwind(AssertUnwindSafe(|| self.handle_sip(&raw, from))).is_err() {
                        self.logf(format!(
                            "gate_sip_server: a SIP handler panicked on a datagram from {from}"
                        ));
                    }
                }
                Err(e) => {
                    // Go returns on any error that is not a timeout (a closed socket). A
                    // transient one (an ICMP port-unreachable surfacing as ECONNREFUSED, a
                    // buffer shortage) must not end the server: retry, and give up only when
                    // the socket keeps failing.
                    if errors.failed() {
                        self.give_up("SIP", &e);
                        return;
                    }
                    tokio::time::sleep(READ_RETRY_PAUSE).await;
                }
            }
        }
    }

    /// A read loop that gave up leaves a deaf server that still looks started: say so through
    /// the error reporter (`driver_error`) and stop it, so `CanDial` goes false and the hub can
    /// start it again. Go returns on its first error just as silently (PARITY 330).
    fn give_up(&self, which: &str, e: &std::io::Error) {
        let msg = format!("the {which} socket failed for good: {e}");
        self.logf(format!("gate_sip_server: {msg}"));
        if let Some(er) = self.events.error_reporter() {
            er.driver_error(&self.cfg.device_id, "socket_failed", &msg);
        }
        let me = self.arc();
        tokio::spawn(async move { me.shutdown().await });
    }

    fn set_err(&self, e: &std::io::Error) {
        lock(&self.state).st.last_err = crate::gojson::io_err(e);
    }

    fn send_bytes(&self, msg: &[u8], addr: SocketAddr) {
        let Some(sock) = lock(&self.sip_send).clone() else {
            return;
        };
        if let Err(e) = sock.send_to(msg, dest_for(self.sip_local.is_ipv6(), addr)) {
            if !self.stop.is_cancelled() {
                self.set_err(&e);
            }
        }
    }

    /// `contactHostPort`: the address the station can actually send to.
    ///
    /// The Contact-address bug (gate-sip main.go reply()): the listen string is a BIND string,
    /// "0.0.0.0:5060", and advertising it verbatim leaves the ACK nowhere to go. The call still
    /// comes up and media still flows; the only symptom is dialogConfirmed staying false forever.
    fn contact_host_port(&self, toward: SocketAddr) -> String {
        let ip = local_ip_toward(toward);
        if ip.is_empty() {
            self.sip_local.to_string()
        } else {
            join_host_port(&ip, self.sip_port)
        }
    }

    // ── answering, hanging up ───────────────────────────────────────────────────────────────

    /// `Snapshot`: the current counters and call state.
    pub fn snapshot(&self) -> Stats {
        let st = lock(&self.state);
        let mut s = st.st.clone();
        s.ringing = st
            .cur
            .as_ref()
            .is_some_and(|c| c.out.is_none() && !c.answered);
        s.dialing = st
            .cur
            .as_ref()
            .is_some_and(|c| c.out.is_some() && !c.answered);
        s.in_call = st.cur.as_ref().is_some_and(|c| c.answered);
        s.dialog_confirmed = st.cur.as_ref().is_some_and(|c| c.confirm);
        s.app_attached = st.client.is_some();
        drop(st);
        let (to_app, from_app, dropped_up, dropped_down) = self.audio_stats.snapshot();
        s.frames_to_app = to_app;
        s.frames_from_app = from_app;
        s.dropped_up = dropped_up;
        s.dropped_down = dropped_down;
        s
    }

    /// `shutdown`: ends a live call politely (BYE if answered, 480 if still ringing), stops every
    /// task and closes the sockets. Returns when the SIP and RTP ports are free.
    pub async fn shutdown(&self) {
        let first = {
            let mut st = lock(&self.state);
            if st.closed {
                None
            } else {
                let c = self.end_cur_locked(&mut st, "the server is stopping");
                st.closed = true;
                if let Some(t) = st.reg_timer.take() {
                    t.cancel();
                }
                Some(c)
            }
        };
        let Some(ended) = first else {
            // Go returns at once; a second caller here waits for the first to finish, so
            // "Stop returned" always means the ports are free
            self.done.cancelled().await;
            return;
        };
        if let Some(c) = ended {
            if c.answered {
                self.send_bye(&c);
            } else if let Some(o) = &c.out {
                self.send_cancel(o); // our own INVITE still ringing out
            } else {
                self.reply_h(
                    &c.h,
                    c.addr,
                    480,
                    "Temporarily Unavailable",
                    &c.tag,
                    &[],
                    "",
                );
            }
        }
        self.stop.cancel();
        // The join and the release of the ports run in a task of their own: a caller whose
        // future is dropped half way cannot leave the others waiting on a shutdown nobody
        // finishes. Every caller (the first included) waits on `done`.
        let me = self.arc();
        tokio::spawn(async move {
            let tasks: Vec<_> = std::mem::take(&mut *lock(&me.tasks));
            let senders: Vec<_> = std::mem::take(&mut *lock(&me.senders));
            for t in tasks.into_iter().chain(senders) {
                let _ = t.await;
            }
            *lock(&me.sip_send) = None;
            *lock(&me.rtp_send) = None;
            me.done.cancel();
        });
        self.done.cancelled().await;
    }
}

impl ServerApi for Server {
    fn ringing_call(&self, call_id: &str) -> bool {
        Server::ringing_call(self, call_id)
    }
    fn attach_for(&self, call_id: &str) -> Result<Arc<AudioClient>, ServerError> {
        Server::attach_for(self, call_id)
    }
    fn hangup_answered(&self, call_id: &str) -> bool {
        Server::hangup_answered(self, call_id)
    }
    fn answer_current(&self) -> Result<(), ServerError> {
        Server::answer_current(self)
    }
    fn can_dial(&self) -> bool {
        Server::can_dial(self)
    }
    fn station(&self) -> (bool, u64) {
        Server::station(self)
    }
    fn snapshot(&self) -> Stats {
        Server::snapshot(self)
    }
    fn dial<'a>(
        &'a self,
        ctx: &'a CancelToken,
        call_id: &'a str,
    ) -> BoxFuture<'a, Result<Arc<AudioClient>, DialError>> {
        Box::pin(Server::dial(self, ctx, call_id))
    }
    fn shutdown(&self) -> BoxFuture<'_, ()> {
        Box::pin(Server::shutdown(self))
    }
}

#[cfg(test)]
mod tests;
