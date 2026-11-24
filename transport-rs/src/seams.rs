//! Test seams (R1-62): hooks the tests use to make production code panic or fail on cue.
//!
//! They exist only with the `test-seams` cargo feature, which the crate's own dev-dependency
//! turns on for `cargo test` and `cargo clippy --all-targets`. A shipped build (`cargo build
//! --release`) carries none of them: [`PanicFlag`] is then a zero-sized type whose `take` is a
//! constant `false`, so the places that consult one cost nothing, not even an atomic load, and the
//! optimiser removes the `panic!("injected ..")` branches behind them (a features-off DEBUG build keeps
//! their text; `scripts/shipped_gate.sh` checks the release binary, R3B-2).
//! (The setters, accessors and the other injection hooks are `#[cfg(feature = "test-seams")]`
//! items on their own types.)

/// A one-shot "panic here on the next pass" switch.
#[derive(Debug, Default)]
pub struct PanicFlag {
    #[cfg(feature = "test-seams")]
    armed: std::sync::atomic::AtomicBool,
}

impl PanicFlag {
    /// Arms the flag: the next [`take`](Self::take) (or [`peek`](Self::peek)) sees it.
    #[cfg(feature = "test-seams")]
    pub fn arm(&self) {
        self.armed.store(true, std::sync::atomic::Ordering::SeqCst);
    }

    /// True once if the flag was armed (and disarms it).
    #[inline]
    pub fn take(&self) -> bool {
        #[cfg(feature = "test-seams")]
        {
            self.armed.swap(false, std::sync::atomic::Ordering::SeqCst)
        }
        #[cfg(not(feature = "test-seams"))]
        {
            false
        }
    }

    /// Whether the flag is armed, without disarming it.
    #[inline]
    pub fn peek(&self) -> bool {
        #[cfg(feature = "test-seams")]
        {
            self.armed.load(std::sync::atomic::Ordering::SeqCst)
        }
        #[cfg(not(feature = "test-seams"))]
        {
            false
        }
    }
}
