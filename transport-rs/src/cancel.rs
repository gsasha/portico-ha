//! A minimal cancellation token (the role of Go's `context.Context` / a closed `stop` channel).
//! See DESIGN.md.

use std::sync::Arc;

use tokio::sync::watch;

/// A clonable, idempotent cancellation flag. All clones share one state.
#[derive(Clone)]
pub struct CancelToken {
    tx: Arc<watch::Sender<bool>>,
}

impl Default for CancelToken {
    fn default() -> Self {
        Self::new()
    }
}

impl CancelToken {
    pub fn new() -> Self {
        let (tx, _rx) = watch::channel(false);
        CancelToken { tx: Arc::new(tx) }
    }

    /// Sets the flag; later calls do nothing.
    pub fn cancel(&self) {
        self.tx.send_replace(true);
    }

    pub fn is_cancelled(&self) -> bool {
        *self.tx.borrow()
    }

    /// Resolves once the token is cancelled (immediately if it already is).
    pub async fn cancelled(&self) {
        let mut rx = self.tx.subscribe();
        loop {
            if *rx.borrow_and_update() {
                return;
            }
            if rx.changed().await.is_err() {
                return;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn cancel_is_idempotent_and_observed_by_every_clone() {
        let t = CancelToken::new();
        let t2 = t.clone();
        assert!(!t.is_cancelled());
        let waiter = tokio::spawn(async move { t2.cancelled().await });
        tokio::task::yield_now().await;
        t.cancel();
        t.cancel();
        waiter.await.unwrap();
        assert!(t.is_cancelled());
        t.cancelled().await;
    }
}
