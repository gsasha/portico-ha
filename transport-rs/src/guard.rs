//! Runs a future under a panic guard without a task of its own (R1-47): the future is polled
//! inside `catch_unwind`, so a panic comes back as `Err(())` and the caller goes on. A `tokio::spawn`
//! per guarded call costs a task (and, for the per-frame downlink, 25 of them a second per leg).
//! The one difference from a spawned task: the guarded future is dropped with the caller, so code
//! that must outlive a caller torn down mid-way spawns its own task (see `wsmedia::Leg::close`).

use std::future::Future;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::pin::Pin;
use std::task::{Context, Poll};

/// The future [`catch`] returns.
pub struct CatchUnwind<F: Future> {
    f: Pin<Box<F>>,
}

impl<F: Future> Future for CatchUnwind<F> {
    type Output = Result<F::Output, ()>;

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        match catch_unwind(AssertUnwindSafe(|| self.f.as_mut().poll(cx))) {
            Ok(Poll::Pending) => Poll::Pending,
            Ok(Poll::Ready(v)) => Poll::Ready(Ok(v)),
            Err(_) => Poll::Ready(Err(())),
        }
    }
}

/// `fut`, with a panic in any poll turned into `Err(())`.
pub fn catch<F: Future>(fut: F) -> CatchUnwind<F> {
    CatchUnwind { f: Box::pin(fut) }
}

/// [`catch`], and a panic is logged on stderr as `portico-transport: <what>: a task panicked`
/// (one place and one stream for the house bridge and the WebSocket legs, R2-156).
pub async fn logged<F: Future<Output = ()>>(what: &str, fut: F) {
    if catch(fut).await.is_err() {
        crate::panic_hook::err_line(&format!("portico-transport: {what}: a task panicked"));
    }
}

#[cfg(test)]
mod tests {
    use super::catch;

    #[tokio::test]
    async fn a_panic_in_the_first_poll_and_in_a_later_one_is_an_err() {
        assert_eq!(catch(async { 7 }).await, Ok(7));
        let r = catch(async {
            panic!("first poll");
        })
        .await;
        assert_eq!(r, Err(()));
        let r = catch(async {
            tokio::task::yield_now().await;
            panic!("second poll");
        })
        .await;
        assert_eq!(r, Err(()));
    }
}
