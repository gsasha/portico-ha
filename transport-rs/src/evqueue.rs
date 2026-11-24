//! A bounded FIFO for events produced faster than a consumer may take them (the server's event
//! queue, the IPC sink's notification queue). Go uses an unbounded slice; here the queue holds at
//! most `cap` items and, when it is full, makes room by evicting the OLDEST low-value item. A
//! critical item (a ring, the end of a call) is never evicted and is only refused when the
//! queue already holds `hard` items, which a consumer that is merely slow cannot reach: it needs
//! `hard` rings and call ends piled up, and every one of them is counted and reported.

use crate::util::lock;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

/// What a push had to throw away.
#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum Dropped {
    /// nothing
    Nothing,
    /// the oldest low-value item made room for the new one
    OldestLowValue,
    /// the new item itself (it is low-value and nothing low-value was queued to evict, or it is
    /// critical and the queue holds `hard` items already)
    Incoming,
}

pub struct EvQueue<T> {
    q: Mutex<VecDeque<T>>,
    cap: usize,
    hard: usize,
    dropped: AtomicU64,
    critical: fn(&T) -> bool,
}

impl<T> EvQueue<T> {
    pub fn new(cap: usize, hard: usize, critical: fn(&T) -> bool) -> Self {
        EvQueue {
            q: Mutex::new(VecDeque::new()),
            cap,
            hard: hard.max(cap),
            dropped: AtomicU64::new(0),
            critical,
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, VecDeque<T>> {
        lock(&self.q)
    }

    /// Queues `item`. Returns what was dropped and the total number of drops so far.
    pub fn push(&self, item: T) -> (Dropped, u64) {
        let mut q = self.lock();
        let mut d = Dropped::Nothing;
        if q.len() >= self.cap {
            let crit = self.critical;
            if let Some(i) = q.iter().position(|x| !crit(x)) {
                q.remove(i);
                d = Dropped::OldestLowValue;
            } else if crit(&item) && q.len() < self.hard {
                // only critical items queued: grow, up to the hard limit
            } else {
                d = Dropped::Incoming;
            }
        }
        if d != Dropped::Incoming {
            q.push_back(item);
        }
        drop(q);
        let total = if d == Dropped::Nothing {
            self.dropped.load(Ordering::SeqCst)
        } else {
            self.dropped.fetch_add(1, Ordering::SeqCst) + 1
        };
        (d, total)
    }

    pub fn pop(&self) -> Option<T> {
        self.lock().pop_front()
    }

    pub fn len(&self) -> usize {
        self.lock().len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::SeqCst)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Debug, PartialEq)]
    enum E {
        Low(u32),
        Crit(u32),
    }

    fn q() -> EvQueue<E> {
        EvQueue::new(4, 6, |e| matches!(e, E::Crit(_)))
    }

    #[test]
    fn full_queue_evicts_the_oldest_low_value_item() {
        let q = q();
        for i in 0..4 {
            assert_eq!(q.push(E::Low(i)).0, Dropped::Nothing);
        }
        assert_eq!(q.push(E::Crit(100)), (Dropped::OldestLowValue, 1));
        assert_eq!(q.pop(), Some(E::Low(1)));
        assert_eq!(q.len(), 3);
    }

    #[test]
    fn critical_items_survive_and_may_exceed_the_cap_up_to_the_hard_limit() {
        let q = q();
        for i in 0..6 {
            assert_eq!(q.push(E::Crit(i)).0, Dropped::Nothing, "{i}");
        }
        assert_eq!(q.push(E::Crit(6)).0, Dropped::Incoming);
        assert_eq!(q.push(E::Low(7)).0, Dropped::Incoming);
        assert_eq!(q.dropped(), 2);
        assert_eq!(q.len(), 6);
    }
}
