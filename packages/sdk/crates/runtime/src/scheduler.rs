//! FIFO conflict-domain scheduling; cancellation drops the queued native future.
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
};
use tokio::sync::{Mutex as AsyncMutex, OwnedMutexGuard};
#[derive(Clone, Default)]
pub struct Scheduler(Arc<Mutex<HashMap<String, Arc<AsyncMutex<()>>>>>);
impl Scheduler {
    pub async fn acquire(&self, domain: &str) -> OwnedMutexGuard<()> {
        let lock = self
            .0
            .lock()
            .unwrap()
            .entry(domain.into())
            .or_default()
            .clone();
        lock.lock_owned().await
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn cancellation_does_not_leak_permit_or_block_other_domains() {
        let scheduler = Scheduler::default();
        let first = scheduler.acquire("phone").await;
        let other = scheduler.acquire("tablet").await;
        assert!(tokio::time::timeout(
            std::time::Duration::from_millis(5),
            scheduler.acquire("phone")
        )
        .await
        .is_err());
        drop(first);
        let _again = scheduler.acquire("phone").await;
        drop(other);
    }
}
