//! Port of `register.go`: keeps one `user#index` account registered on a shared [`Endpoint`],
//! re-registering at 50% of the granted Expires and de-registering cleanly (Expires: 0) on
//! cancel (the session-quota logout discipline).

use std::time::Duration;

use tokio::time::sleep;

use super::digest::{
    build_authorization_header_raw, digest_response_raw, parse_digest_challenge_raw,
};
use super::endpoint::Endpoint;
use super::message::{build_register, RegisterParams};
use super::Error;
use crate::cancel::CancelToken;
use crate::gostd::net::local_host;
use crate::gostd::strconv::go_atoi;
use crate::util::{rand_f64, rand_hex};

/// Failed re-registrations back off exponentially from `RETRY_BASE` up to `RETRY_MAX`, with
/// jitter, instead of hammering the station.
pub const RETRY_BASE: Duration = Duration::from_secs(5);
pub const RETRY_MAX: Duration = Duration::from_secs(5 * 60);

/// The wait before re-registration attempt number `failures` (1 = first failure): `RETRY_BASE`
/// doubling per failure, capped at `RETRY_MAX`, with +/-20% jitter.
pub fn retry_delay(failures: i64) -> Duration {
    let max = RETRY_MAX.as_nanos() as i64;
    let mut d = max;
    if failures < 16 {
        // Go: `retryBase << uint(failures-1)`; a negative count becomes a huge shift, which is 0.
        let shift = (failures - 1) as u64;
        let v = if shift >= 64 {
            0
        } else {
            (RETRY_BASE.as_nanos() as i64) << shift
        };
        if v < max {
            d = v;
        }
    }
    let jittered = (d as f64 * (0.8 + 0.4 * rand_f64())) as i64;
    Duration::from_nanos(jittered.max(0) as u64)
}

type StateFn = Box<dyn Fn(bool, Option<&Error>) + Send + Sync>;
type WaitFn = Box<dyn Fn(i64) -> Duration + Send + Sync>;

/// Keeps one account registered. One per SIP sub-account, all sharing ONE [`Endpoint`] per
/// device (a device has one SIP listening address).
pub struct Registrar {
    endpoint: Endpoint,
    username: String,
    password: String,
    expires: i64,
    retry_wait: Option<WaitFn>,
    /// Fires on every registration attempt's outcome (including each failed initial retry).
    pub on_state_change: Option<StateFn>,
}

impl Registrar {
    /// `NewRegistrar`: `expires` 0 means 3600.
    pub fn new(endpoint: &Endpoint, username: &str, password: &str, expires: i64) -> Registrar {
        Registrar {
            endpoint: endpoint.clone(),
            username: username.into(),
            password: password.into(),
            expires: if expires == 0 { 3600 } else { expires },
            retry_wait: None,
            on_state_change: None,
        }
    }

    /// Overrides [`retry_delay`] for the initial-registration retries (tests only).
    #[cfg(feature = "test-seams")]
    pub fn set_retry_wait(&mut self, f: impl Fn(i64) -> Duration + Send + Sync + 'static) {
        self.retry_wait = Some(Box::new(f));
    }

    fn notify(&self, ok: bool, err: Option<&Error>) {
        if let Some(f) = &self.on_state_change {
            f(ok, err);
        }
    }

    /// Registers once, then loops re-registering until `ctx` is cancelled, then sends a final
    /// Expires: 0 REGISTER and returns `Ok`. A failed FIRST registration is retried with
    /// [`retry_delay`]'s backoff, each outcome reported through `on_state_change`; `run` returns
    /// early only for a definitive refusal (486 Busy Here, auth rejected) or when `ctx` is done.
    pub async fn run(&self, ctx: &CancelToken) -> Result<(), Error> {
        let call_id = format!("{}@{}", rand_hex(8), local_host(self.endpoint.local_addr()));
        let from_tag = rand_hex(8);
        let mut cseq: i64 = 1;

        let mut granted: i64;
        let mut attempt: i64 = 1;
        loop {
            let r = self.register(cseq, &call_id, &from_tag, self.expires).await;
            cseq += 2; // register() may itself send a second, authenticated request
            match r {
                Ok(g) => {
                    granted = g;
                    self.notify(true, None);
                    break;
                }
                Err(err) => {
                    self.notify(false, Some(&err));
                    if err.is_busy_here() || err.is_auth_rejected() {
                        return Err(err); // definitive: retrying would hammer the quota
                    }
                    let wait = match &self.retry_wait {
                        Some(f) => f(attempt),
                        None => retry_delay(attempt),
                    };
                    tokio::select! {
                        _ = ctx.cancelled() => return Ok(()), // never registered
                        _ = sleep(wait) => {}
                    }
                }
            }
            attempt += 1;
        }

        let mut failures: i64 = 0;
        loop {
            // granted keeps the last successful value across failures, so a transient error does
            // not collapse the cadence; failures switch to exponential backoff below. (Go's
            // `Duration(granted) * time.Second` wraps for absurd values; so does this.)
            let mut wait = renew_wait(granted);
            if failures > 0 {
                wait = retry_delay(failures);
            }
            tokio::select! {
                _ = ctx.cancelled() => {
                    // best-effort clean de-register
                    let _ = self.register(cseq, &call_id, &from_tag, 0).await;
                    return Ok(());
                }
                _ = sleep(wait) => {
                    let r = self.register(cseq, &call_id, &from_tag, self.expires).await;
                    cseq += 2;
                    match r {
                        Ok(g) => {
                            self.notify(true, None);
                            granted = g;
                            failures = 0;
                        }
                        Err(err) => {
                            self.notify(false, Some(&err));
                            if err.is_busy_here() || err.is_auth_rejected() {
                                return Err(err);
                            }
                            failures += 1;
                        }
                    }
                }
            }
        }
    }

    /// One REGISTER, following a single 401/407 challenge with a digest-authenticated retry.
    /// Returns the granted Expires from the final 200 OK.
    async fn register(
        &self,
        cseq: i64,
        call_id: &str,
        from_tag: &str,
        expires: i64,
    ) -> Result<i64, Error> {
        let ep = &self.endpoint;
        let branch = format!("z9hG4bK{}", rand_hex(8));
        let mut resp = ep
            .send_request(
                &build_register(&RegisterParams {
                    host: ep.host().into(),
                    local_addr: ep.local_addr().into(),
                    username: self.username.clone(),
                    call_id: call_id.into(),
                    from_tag: from_tag.into(),
                    cseq,
                    expires,
                    branch: branch.clone(),
                    ..Default::default()
                }),
                &branch,
                None,
            )
            .await?;

        if resp.status_code == 401 || resp.status_code == 407 {
            let header_name = if resp.status_code == 407 {
                "proxy-authenticate"
            } else {
                "www-authenticate"
            };
            // The challenge is read, hashed and echoed as the bytes the station sent: it hashes
            // its own bytes, so a UTF-8 repair would give a second 401 (R1-5). Go repairs each
            // invalid byte to U+FFFD and so cannot register against such a station (PARITY 420).
            let challenge = parse_digest_challenge_raw(resp.header_raw(header_name))
                .map_err(|e| Error::msg(format!("sip: parsing {header_name}: {e}")))?;
            let cnonce = rand_hex(8);
            let uri = format!("sip:{}", ep.host());
            let auth_resp = digest_response_raw(
                self.username.as_bytes(),
                self.password.as_bytes(),
                b"REGISTER",
                uri.as_bytes(),
                &challenge,
                cnonce.as_bytes(),
                1,
            );
            let auth = build_authorization_header_raw(
                self.username.as_bytes(),
                uri.as_bytes(),
                &challenge,
                &auth_resp,
                cnonce.as_bytes(),
                1,
            );
            let branch2 = format!("z9hG4bK{}", rand_hex(8));
            resp = ep
                .send_request(
                    &build_register(&RegisterParams {
                        host: ep.host().into(),
                        local_addr: ep.local_addr().into(),
                        username: self.username.clone(),
                        call_id: call_id.into(),
                        from_tag: from_tag.into(),
                        cseq: cseq + 1,
                        expires,
                        auth_header: auth,
                        branch: branch2.clone(),
                    }),
                    &branch2,
                    None,
                )
                .await?;
        }

        match resp.status_code {
            200 => {
                let e = resp.header("Expires");
                if !e.is_empty() {
                    if let Ok(v) = go_atoi(e) {
                        return Ok(v);
                    }
                }
                Ok(expires)
            }
            486 => Err(Error::BusyHere),
            401 | 403 | 407 => Err(Error::AuthRejected(format!(
                "{} {}",
                resp.status_code, resp.reason
            ))),
            _ => Err(Error::msg(format!(
                "sip: REGISTER failed: {} {}",
                resp.status_code, resp.reason
            ))),
        }
    }
}

/// Longest Expires honoured from a registrar's reply, in seconds (one day).
const MAX_GRANTED_SECS: i64 = 86_400;

/// Time to wait before re-registering: half the granted Expires, floored at 1 s.
fn renew_wait(granted: i64) -> Duration {
    // DEV-796 S9: the registrar controls `granted`; clamp it so the product cannot wrap. The
    // lower bound avoids a busy loop on a pathologically short Expires.
    let granted = granted.clamp(1, MAX_GRANTED_SECS);
    Duration::from_nanos((granted * 1_000_000_000 / 2) as u64).max(Duration::from_secs(1))
}

#[cfg(test)]
mod tests {
    use super::*;

    // DEV-796 S9: a registrar-controlled Expires must not wrap the renewal cadence.
    #[test]
    fn renew_wait_clamps_hostile_expires() {
        let cap = Duration::from_secs(43_200);
        for g in [i64::MAX, 9_223_372_036, 18_446_744_076, i64::MIN, -5, 0, 1] {
            let w = renew_wait(g);
            assert!(
                w >= Duration::from_secs(1) && w <= cap,
                "granted {g} -> {w:?}"
            );
        }
        assert_eq!(renew_wait(120), Duration::from_secs(60));
    }
    use std::sync::atomic::{AtomicI64, Ordering};
    use std::sync::{Arc, Mutex};
    use tokio::net::UdpSocket;
    use tokio::sync::mpsc;
    use tokio::time::timeout;

    use crate::sip::endpoint::tests::via_line;

    /// Spawns a UDP server answering each datagram with `f(request, count_so_far)`.
    async fn serve<F>(f: F) -> (std::net::SocketAddr, Arc<AtomicI64>)
    where
        F: Fn(&str, i64) -> Option<String> + Send + Sync + 'static,
    {
        let sock = Arc::new(UdpSocket::bind("127.0.0.1:0").await.unwrap());
        let addr = sock.local_addr().unwrap();
        let n = Arc::new(AtomicI64::new(0));
        let n2 = n.clone();
        tokio::spawn(async move {
            let mut buf = vec![0u8; 4096];
            while let Ok((k, raddr)) = sock.recv_from(&mut buf).await {
                let req = String::from_utf8_lossy(&buf[..k]).into_owned();
                let c = n2.fetch_add(1, Ordering::SeqCst) + 1;
                if let Some(resp) = f(&req, c) {
                    let _ = sock.send_to(resp.as_bytes(), raddr).await;
                }
            }
        });
        (addr, n)
    }

    async fn endpoint_for(addr: std::net::SocketAddr) -> Endpoint {
        Endpoint::new(&addr.ip().to_string(), i64::from(addr.port()))
            .await
            .unwrap()
    }

    // TestRegistrar_ChallengeResponseAndCleanDeregister
    #[tokio::test]
    async fn registrar_challenge_response_and_clean_deregister() {
        let (dereg_tx, mut dereg_rx) = mpsc::unbounded_channel::<()>();
        let (addr, _) = serve(move |req, _| {
            let via = via_line(req);
            Some(if !req.contains("Authorization:") {
                format!("SIP/2.0 401 Unauthorized\r\n{via}WWW-Authenticate: Digest realm=\"test-realm\", nonce=\"test-nonce-1234\", algorithm=MD5\r\nContent-Length: 0\r\n\r\n")
            } else if req.contains("Expires: 0") {
                let _ = dereg_tx.send(());
                format!("SIP/2.0 200 OK\r\n{via}Expires: 0\r\nContent-Length: 0\r\n\r\n")
            } else {
                format!("SIP/2.0 200 OK\r\n{via}Expires: 2\r\nContent-Length: 0\r\n\r\n")
            })
        })
        .await;
        let ep = endpoint_for(addr).await;
        let (stx, mut srx) = mpsc::unbounded_channel::<bool>();
        // Expires=2 is short deliberately, so the keepalive loop re-registers within the budget.
        let mut r = Registrar::new(&ep, "alice", "secret", 2);
        r.on_state_change = Some(Box::new(move |ok, _| {
            let _ = stx.send(ok);
        }));
        let r = Arc::new(r);
        let ctx = CancelToken::new();
        let (r2, c2) = (r.clone(), ctx.clone());
        let done = tokio::spawn(async move { r2.run(&c2).await });

        let first = timeout(Duration::from_secs(3), srx.recv())
            .await
            .expect("timed out waiting for first registration")
            .unwrap();
        assert!(first, "first registration did not succeed");
        let second = timeout(Duration::from_secs(3), srx.recv())
            .await
            .expect("timed out waiting for keepalive re-registration")
            .unwrap();
        assert!(second, "keepalive re-registration did not succeed");

        ctx.cancel();
        let res = timeout(Duration::from_secs(3), done)
            .await
            .expect("timed out waiting for Run to return after cancel")
            .unwrap();
        assert_eq!(res, Ok(()), "Run returned error after cancel");
        timeout(Duration::from_secs(1), dereg_rx.recv())
            .await
            .expect("no clean de-register (Expires: 0) was sent on shutdown");
    }

    /// R2-16: runs a Registrar on the paused clock against a station that grants `granted` and
    /// returns the instants (relative to the first REGISTER) of the first `n` REGISTERs.
    async fn register_instants(requested: i64, granted: i64, n: usize) -> Vec<Duration> {
        let (tx, mut rx) = mpsc::unbounded_channel::<tokio::time::Instant>();
        let (addr, _) = serve(move |req, _| {
            if req.contains("Expires: 0") {
                return None; // the shutdown de-register: no answer needed
            }
            let _ = tx.send(tokio::time::Instant::now());
            Some(format!(
                "SIP/2.0 200 OK\r\n{}Expires: {granted}\r\nContent-Length: 0\r\n\r\n",
                via_line(req)
            ))
        })
        .await;
        let ep = endpoint_for(addr).await;
        let r = Arc::new(Registrar::new(&ep, "alice", "secret", requested));
        let ctx = CancelToken::new();
        let (r2, c2) = (r.clone(), ctx.clone());
        let done = tokio::spawn(async move { r2.run(&c2).await });
        let mut at = Vec::new();
        while at.len() < n {
            at.push(rx.recv().await.unwrap());
        }
        ctx.cancel();
        let _ = done.await;
        at.iter().map(|t| *t - at[0]).collect()
    }

    // R2-16. The cadence is HALF of the GRANTED Expires (not the requested one, not 100%, which
    // would let the registration lapse on every cycle), with a 1 s floor. The Go test only checks
    // that a 2 s grant is re-registered within 3 s, which 1 s, 2 s and 3 s all satisfy.
    #[tokio::test(start_paused = true)]
    async fn re_registers_at_half_of_the_granted_expires() {
        let at = register_instants(60, 10, 3).await;
        // requested 60, granted 10: 5 s apart, twice
        assert_eq!(
            at,
            vec![
                Duration::ZERO,
                Duration::from_secs(5),
                Duration::from_secs(10)
            ]
        );
    }

    #[tokio::test(start_paused = true)]
    async fn re_register_interval_has_a_one_second_floor() {
        let at = register_instants(60, 1, 3).await;
        assert_eq!(
            at,
            vec![
                Duration::ZERO,
                Duration::from_secs(1),
                Duration::from_secs(2)
            ]
        );
        let at = register_instants(60, 0, 2).await;
        assert_eq!(at, vec![Duration::ZERO, Duration::from_secs(1)]);
    }

    // TestRegistrar_BusyHereStopsRetrying
    #[tokio::test]
    async fn registrar_busy_here_stops_retrying() {
        let (addr, _) = serve(|req, _| {
            Some(format!(
                "SIP/2.0 486 Busy Here\r\n{}Content-Length: 0\r\n\r\n",
                via_line(req)
            ))
        })
        .await;
        let ep = endpoint_for(addr).await;
        let r = Registrar::new(&ep, "bob", "x", 0);
        let err = r
            .run(&CancelToken::new())
            .await
            .expect_err("expected an error");
        assert_eq!(err, Error::BusyHere);
    }

    // TestRetryDelay_BacksOffWithCap
    #[test]
    fn retry_delay_backs_off_with_cap() {
        let lo = RETRY_BASE.mul_f64(0.8);
        let hi = RETRY_MAX.mul_f64(1.2);
        let mut last = Duration::ZERO;
        for f in 1..=40 {
            let d = retry_delay(f);
            assert!(d >= lo && d <= hi, "retryDelay({f})={d:?} out of bounds");
            if f == 1 {
                assert!(d <= RETRY_BASE.mul_f64(1.2), "first retry {d:?}");
            }
            last = d;
        }
        assert!(
            last >= RETRY_MAX.mul_f64(0.8),
            "delay did not reach the cap: {last:?}"
        );
    }

    // TestRegistrar_AuthRejectedIsDefinitive
    #[tokio::test]
    async fn registrar_auth_rejected_is_definitive() {
        for status in ["401 Unauthorized", "403 Forbidden"] {
            let (addr, _) = serve(move |req, _| {
                let via = via_line(req);
                Some(if status.starts_with("401") {
                    format!("SIP/2.0 401 Unauthorized\r\n{via}WWW-Authenticate: Digest realm=\"r\", nonce=\"n\", algorithm=MD5\r\nContent-Length: 0\r\n\r\n")
                } else {
                    format!("SIP/2.0 {status}\r\n{via}Content-Length: 0\r\n\r\n")
                })
            })
            .await;
            let ep = endpoint_for(addr).await;
            let r = Registrar::new(&ep, "bob", "wrong", 0);
            let err = r.run(&CancelToken::new()).await.expect_err(status);
            assert!(err.is_auth_rejected(), "{status}: {err}");
        }
    }

    // TestRegistrar_ReregisterFailureBacksOff
    #[tokio::test]
    async fn registrar_reregister_failure_backs_off() {
        let (addr, count) = serve(|req, c| {
            let via = via_line(req);
            Some(if c == 1 {
                format!("SIP/2.0 200 OK\r\n{via}Expires: 2\r\nContent-Length: 0\r\n\r\n")
            } else {
                format!("SIP/2.0 500 Server Error\r\n{via}Content-Length: 0\r\n\r\n")
            })
        })
        .await;
        let ep = endpoint_for(addr).await;
        let ctx = CancelToken::new();
        let c2 = ctx.clone();
        tokio::spawn(async move {
            sleep(Duration::from_secs(6)).await;
            c2.cancel();
        });
        let _ = Registrar::new(&ep, "bob", "x", 2).run(&ctx).await;
        // t=0 success; t=1 first failure; then >= ~4 s backoff. Old code: ~6 attempts.
        let n = count.load(Ordering::SeqCst);
        assert!(
            n <= 4,
            "{n} REGISTERs in 6 s; expected backoff (<= 4 incl. de-register)"
        );
    }

    /// startScriptedDevice: answers each REGISTER with the next status of `codes` (the last
    /// repeats); a 200 carries Expires: 3600.
    async fn scripted_device(codes: Vec<i64>) -> (std::net::SocketAddr, Arc<AtomicI64>) {
        serve(move |req, c| {
            let i = ((c - 1) as usize).min(codes.len() - 1);
            let code = codes[i];
            let line = match code {
                200 => "OK",
                403 => "Forbidden",
                500 => "Server Error",
                _ => "",
            };
            Some(format!(
                "SIP/2.0 {code} {line}\r\n{}Expires: 3600\r\nContent-Length: 0\r\n\r\n",
                via_line(req)
            ))
        })
        .await
    }

    async fn test_registrar(addr: std::net::SocketAddr) -> Registrar {
        let ep = endpoint_for(addr).await;
        let mut r = Registrar::new(&ep, "alice", "secret", 3600);
        r.set_retry_wait(|_| Duration::from_millis(10));
        r
    }

    // TestRegistrar_RetriesFailedInitialRegistration
    #[tokio::test]
    async fn registrar_retries_failed_initial_registration() {
        let (addr, n) = scripted_device(vec![500, 500, 200]).await;
        let mut r = test_registrar(addr).await;
        let states: Arc<Mutex<Vec<bool>>> = Arc::default();
        let (reg_tx, mut reg_rx) = mpsc::unbounded_channel::<()>();
        let st = states.clone();
        r.on_state_change = Some(Box::new(move |ok, _| {
            st.lock().unwrap().push(ok);
            if ok {
                let _ = reg_tx.send(());
            }
        }));
        let r = Arc::new(r);
        let ctx = CancelToken::new();
        let (r2, c2) = (r.clone(), ctx.clone());
        let done = tokio::spawn(async move { r2.run(&c2).await });
        timeout(Duration::from_secs(3), reg_rx.recv())
            .await
            .expect("never registered after the initial failures");
        assert_eq!(*states.lock().unwrap(), vec![false, false, true]);
        ctx.cancel();
        assert_eq!(done.await.unwrap(), Ok(()), "Run after cancel");
        assert_eq!(n.load(Ordering::SeqCst), 4, "3 attempts + the Expires: 0");
    }

    // TestRegistrar_CancelStopsInitialRetry
    #[tokio::test]
    async fn registrar_cancel_stops_initial_retry() {
        let (addr, _) = scripted_device(vec![500]).await;
        let r = Arc::new(test_registrar(addr).await);
        let ctx = CancelToken::new();
        let (r2, c2) = (r.clone(), ctx.clone());
        let done = tokio::spawn(async move { r2.run(&c2).await });
        sleep(Duration::from_millis(100)).await;
        ctx.cancel();
        let res = timeout(Duration::from_secs(2), done)
            .await
            .expect("Run did not return after cancel")
            .unwrap();
        assert_eq!(res, Ok(()));
    }

    // TestRegistrar_DefinitiveFailureIsNotRetried (subtests 403, 486)
    #[tokio::test]
    async fn registrar_definitive_failure_is_not_retried() {
        for (code, busy) in [(403, false), (486, true)] {
            let (addr, n) = scripted_device(vec![code]).await;
            let r = test_registrar(addr).await;
            let err = r.run(&CancelToken::new()).await.expect_err("definitive");
            assert!(
                if busy {
                    err.is_busy_here()
                } else {
                    err.is_auth_rejected()
                },
                "{code}: {err}"
            );
            assert_eq!(n.load(Ordering::SeqCst), 1, "device saw extra REGISTERs");
        }
    }

    // R1-5: a challenge with a non-UTF-8 realm/nonce byte is answered over the raw bytes, so the
    // station accepts it (a UTF-8 repair would give a second 401, which is what Go gets: PARITY 420).
    #[tokio::test]
    async fn register_answers_a_non_utf8_challenge_over_raw_bytes() {
        let sock = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let addr = sock.local_addr().unwrap();
        let (tx, mut rx) = mpsc::unbounded_channel::<Vec<u8>>();
        tokio::spawn(async move {
            let mut buf = vec![0u8; 4096];
            while let Ok((n, from)) = sock.recv_from(&mut buf).await {
                let req = buf[..n].to_vec();
                let via: Vec<u8> = req
                    .split(|&c| c == b'\n')
                    .find(|l| l.starts_with(b"Via:"))
                    .unwrap()
                    .to_vec();
                let mut r = if req.windows(14).any(|w| w == b"Authorization:") {
                    let _ = tx.send(req.clone());
                    b"SIP/2.0 200 OK\r\n".to_vec()
                } else {
                    b"SIP/2.0 401 Unauthorized\r\n".to_vec()
                };
                r.extend_from_slice(&via);
                r.push(b'\n');
                if r.starts_with(b"SIP/2.0 401") {
                    r.extend_from_slice(b"WWW-Authenticate: Digest realm=\"re\xe9alm\", nonce=\"n\xffn1\", algorithm=MD5\r\n");
                }
                r.extend_from_slice(b"Content-Length: 0\r\n\r\n");
                let _ = sock.send_to(&r, from).await;
            }
        });
        let ep = Endpoint::new(&addr.ip().to_string(), i64::from(addr.port()))
            .await
            .unwrap();
        let reg = Registrar::new(&ep, "alice", "secret", 60);
        reg.register(1, "cid@h", "ftag", 60).await.unwrap();
        let second = rx.recv().await.unwrap();
        let has = |n: &[u8]| second.windows(n.len()).any(|w| w == n);
        // hash of "alice:re\xe9alm:secret" etc. computed over the raw bytes with hashlib
        assert!(
            has(b"realm=\"re\xe9alm\", nonce=\"n\xffn1\""),
            "{:?}",
            String::from_utf8_lossy(&second)
        );
        assert!(
            has(b"response=\"740147e1c1b4555559a6b48ccf90cb99\""),
            "{:?}",
            String::from_utf8_lossy(&second)
        );
    }
}
