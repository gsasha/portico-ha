//! Connecting to a host name the way Go's `net.Dialer` does (R1-8): `dialParallel` and
//! `dialSerial`.
//!
//! A host name that resolves to several addresses is not tried one after another until the whole
//! deadline is gone. The addresses are split by family (the family of the first one is the
//! "primary"); the primary family is tried address by address, each with a share of what is left
//! of the deadline (`partialDeadline`: `remaining / addresses_left`, but at least 2 s while that
//! much is left); if the primary family has not connected after 300 ms (Go's `FallbackDelay`),
//! or has failed, the other family is tried the same way at the same time, and the first
//! connection wins. When both fail, the error is the primary's first one.
//!
//! The race itself is generic over the connect function, so its timing is tested without a
//! network (`start_paused` tests below).

use std::future::Future;
use std::io;
use std::net::SocketAddr;
use std::time::Duration;

use tokio::net::TcpStream;
use tokio::time::Instant;

/// `net.Dialer.FallbackDelay` default.
pub const FALLBACK_DELAY: Duration = Duration::from_millis(300);
/// `partialDeadline`'s `saneMinimum`.
const SANE_MINIMUM: Duration = Duration::from_secs(2);

/// `partialDeadline`: the share of the remaining time one of `addrs_remaining` addresses gets.
fn partial_deadline(now: Instant, deadline: Instant, addrs_remaining: usize) -> Instant {
    let remaining = deadline.saturating_duration_since(now);
    let n = u32::try_from(addrs_remaining.max(1)).unwrap_or(u32::MAX);
    let mut share = remaining / n;
    if share < SANE_MINIMUM {
        share = remaining.min(SANE_MINIMUM);
    }
    now + share
}

type Failure = (SocketAddr, io::Error);

/// `dialSerial`: each address in turn, with its share of the deadline; the first error is the
/// one reported.
async fn dial_serial<T, F, Fut>(
    addrs: &[SocketAddr],
    deadline: Instant,
    connect: &F,
) -> Result<T, Failure>
where
    F: Fn(SocketAddr) -> Fut,
    Fut: Future<Output = io::Result<T>>,
{
    let mut first: Option<Failure> = None;
    for (i, a) in addrs.iter().enumerate() {
        let now = Instant::now();
        if now >= deadline {
            break;
        }
        let until = partial_deadline(now, deadline, addrs.len() - i);
        let err = match tokio::time::timeout_at(until, connect(*a)).await {
            Ok(Ok(v)) => return Ok(v),
            Ok(Err(e)) => e,
            Err(_) => io::Error::from(io::ErrorKind::TimedOut),
        };
        if first.is_none() {
            first = Some((*a, err));
        }
    }
    Err(first.unwrap_or_else(|| {
        (
            addrs[0],
            io::Error::from(io::ErrorKind::TimedOut), // the deadline was already gone
        )
    }))
}

/// `dialParallel` over `addrs` (not empty). `connect` makes one attempt at one address.
pub async fn connect_any<T, F, Fut>(
    addrs: &[SocketAddr],
    deadline: Instant,
    connect: F,
) -> Result<T, Failure>
where
    F: Fn(SocketAddr) -> Fut,
    Fut: Future<Output = io::Result<T>>,
{
    let primary_v4 = addrs[0].is_ipv4();
    let (primaries, fallbacks): (Vec<SocketAddr>, Vec<SocketAddr>) =
        addrs.iter().partition(|a| a.is_ipv4() == primary_v4);
    if fallbacks.is_empty() {
        return dial_serial(&primaries, deadline, &connect).await;
    }
    let primary = dial_serial(&primaries, deadline, &connect);
    tokio::pin!(primary);
    let fallback = dial_serial(&fallbacks, deadline, &connect);
    tokio::pin!(fallback);
    // the primary alone for the fallback delay, or until it has failed
    let first_failure = tokio::select! {
        r = &mut primary => match r {
            Ok(v) => return Ok(v),
            Err(e) => Some(e),
        },
        _ = tokio::time::sleep(FALLBACK_DELAY) => None,
    };
    match first_failure {
        // the primary failed early: the fallback starts at once; its error does not matter
        Some(pe) => fallback.await.map_err(|_| pe),
        None => {
            let (mut p_err, mut f_done) = (None, false);
            let mut p_done = false;
            loop {
                tokio::select! {
                    r = &mut primary, if !p_done => match r {
                        Ok(v) => return Ok(v),
                        Err(e) => { p_err = Some(e); p_done = true; }
                    },
                    r = &mut fallback, if !f_done => match r {
                        Ok(v) => return Ok(v),
                        Err(_) => f_done = true,
                    },
                }
                if p_done && f_done {
                    return Err(p_err.expect("primary finished"));
                }
            }
        }
    }
}

/// `connect_any` over real TCP.
pub async fn connect_tcp(addrs: &[SocketAddr], deadline: Instant) -> Result<TcpStream, Failure> {
    connect_any(addrs, deadline, TcpStream::connect).await
}

/// `net.Dialer{Timeout}.DialContext("tcp", host:port)` for the audio-socket legs: resolve and
/// connect under one deadline. Only success matters to the callers (their text is fixed).
pub async fn dial_host(hostport: &str, within: Duration) -> io::Result<TcpStream> {
    let deadline = Instant::now() + within;
    let work = async {
        let addrs: Vec<SocketAddr> = tokio::net::lookup_host(hostport).await?.collect();
        if addrs.is_empty() {
            return Err(io::Error::new(io::ErrorKind::NotFound, "no such host"));
        }
        connect_tcp(&addrs, deadline).await.map_err(|(_, e)| e)
    };
    match tokio::time::timeout_at(deadline, work).await {
        Ok(r) => r,
        Err(_) => Err(io::Error::from(io::ErrorKind::TimedOut)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn a4(p: u16) -> SocketAddr {
        SocketAddr::from(([192, 0, 2, 1], p))
    }
    fn a6(p: u16) -> SocketAddr {
        SocketAddr::from(([0x2001, 0xdb8, 0, 0, 0, 0, 0, 1], p))
    }

    // R1-8: a black-holed first address (never answers) and a working one of the other family:
    // the working one is reached after the 300 ms head start, not after the whole deadline.
    #[tokio::test(start_paused = true)]
    async fn a_black_holed_primary_is_overtaken_after_the_fallback_delay() {
        let t0 = Instant::now();
        let r = connect_any(
            &[a4(1), a6(2)],
            t0 + Duration::from_secs(3),
            |a| async move {
                if a.port() == 1 {
                    std::future::pending::<io::Result<u16>>().await
                } else {
                    Ok(a.port())
                }
            },
        )
        .await;
        assert_eq!(r.ok(), Some(2));
        assert_eq!(
            t0.elapsed(),
            Duration::from_millis(300),
            "Go's Dialer.FallbackDelay"
        );
    }

    // The primary that fails at once hands over to the fallback with no wait.
    #[tokio::test(start_paused = true)]
    async fn a_refusing_primary_hands_over_at_once() {
        let t0 = Instant::now();
        let r = connect_any(
            &[a4(1), a6(2)],
            t0 + Duration::from_secs(3),
            |a| async move {
                if a.port() == 1 {
                    Err(io::Error::from(io::ErrorKind::ConnectionRefused))
                } else {
                    Ok(a.port())
                }
            },
        )
        .await;
        assert_eq!(r.ok(), Some(2));
        assert_eq!(t0.elapsed(), Duration::ZERO);
    }

    // Same family: the first address gets a share of the deadline (half of 6 s), so the second
    // is still tried; before R1-8 the first one consumed all of it.
    #[tokio::test(start_paused = true)]
    async fn the_deadline_is_shared_between_the_addresses_of_one_family() {
        let t0 = Instant::now();
        let r = connect_any(
            &[a4(1), a4(2)],
            t0 + Duration::from_secs(6),
            |a| async move {
                if a.port() == 1 {
                    std::future::pending::<io::Result<u16>>().await
                } else {
                    Ok(a.port())
                }
            },
        )
        .await;
        assert_eq!(r.ok(), Some(2));
        assert_eq!(t0.elapsed(), Duration::from_secs(3));
    }

    // Both fail: the primary's first error (and its address) is the one reported.
    #[tokio::test(start_paused = true)]
    async fn when_both_fail_the_primary_error_is_reported() {
        let t0 = Instant::now();
        let r = connect_any::<u16, _, _>(
            &[a4(1), a6(2)],
            t0 + Duration::from_secs(3),
            |a| async move {
                if a.port() == 1 {
                    Err(io::Error::from(io::ErrorKind::ConnectionRefused))
                } else {
                    Err(io::Error::from(io::ErrorKind::TimedOut))
                }
            },
        )
        .await;
        let (addr, e) = r.err().unwrap();
        assert_eq!(
            (addr.port(), e.kind()),
            (1, io::ErrorKind::ConnectionRefused)
        );
    }

    // R2-26: the fallback starts when the primary has FAILED, not after the full delay.
    #[tokio::test(start_paused = true)]
    async fn a_primary_that_fails_at_100_ms_starts_the_fallback_at_100_ms() {
        let t0 = Instant::now();
        let r = connect_any(
            &[a4(1), a6(2)],
            t0 + Duration::from_secs(3),
            |a| async move {
                if a.port() == 1 {
                    tokio::time::sleep(Duration::from_millis(100)).await;
                    Err(io::Error::from(io::ErrorKind::ConnectionRefused))
                } else {
                    Ok(a.port())
                }
            },
        )
        .await;
        assert_eq!(r.ok(), Some(2));
        assert_eq!(t0.elapsed(), Duration::from_millis(100));
    }

    // R2-6: a connect that never completes ends in a `TimedOut` of the dial itself, at the
    // deadline, before an outer `timeout_at` of the same instant can answer (one address, and two
    // of one family); `HttpClient::dial` words that as the context error (`dial_failure`).
    #[tokio::test(start_paused = true)]
    async fn a_connect_that_never_completes_times_out_at_the_deadline() {
        for addrs in [vec![a4(1)], vec![a4(1), a4(2)]] {
            let t0 = Instant::now();
            let deadline = t0 + Duration::from_secs(5);
            let r = tokio::time::timeout_at(
                deadline,
                connect_any::<u16, _, _>(&addrs, deadline, |_| std::future::pending()),
            )
            .await;
            let (addr, e) = r.expect("the dial answers first").err().unwrap();
            assert_eq!((addr.port(), e.kind()), (1, io::ErrorKind::TimedOut));
            assert_eq!(t0.elapsed(), Duration::from_secs(5));
        }
    }

    #[test]
    fn partial_deadline_is_go_s() {
        let now = Instant::now();
        let at = |s: u64| now + Duration::from_secs(s);
        assert_eq!(partial_deadline(now, at(6), 2), at(3));
        assert_eq!(partial_deadline(now, at(6), 1), at(6));
        // a share under 2 s becomes 2 s while that much is left
        assert_eq!(partial_deadline(now, at(6), 5), at(2));
        // less than 2 s left: all of it
        assert_eq!(partial_deadline(now, at(1), 4), at(1));
    }

    #[tokio::test]
    async fn dial_host_connects_over_loopback() {
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap().to_string();
        assert!(dial_host(&addr, Duration::from_secs(2)).await.is_ok());
        drop(l);
        assert!(dial_host(&addr, Duration::from_secs(2)).await.is_err());
    }
}
