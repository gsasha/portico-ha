//! `manager.go`: cross-call arbitration, one call per device, "already answered" races.

use crate::util::lock;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use super::state_machine::{State, StateMachine};

/// `ErrAlreadyAnswered` / `ErrDeviceBusy`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ManagerError {
    /// "call: already answered"
    AlreadyAnswered,
    /// "call: device busy"
    DeviceBusy,
}

impl ManagerError {
    pub fn message(self) -> &'static str {
        match self {
            ManagerError::AlreadyAnswered => "call: already answered",
            ManagerError::DeviceBusy => "call: device busy",
        }
    }
}

impl std::fmt::Display for ManagerError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.message())
    }
}

/// Every active call's arbitration state, keyed by device.
#[derive(Default)]
pub struct Manager {
    calls: Mutex<HashMap<String, Arc<StateMachine>>>,
}

impl Manager {
    pub fn new() -> Self {
        Self::default()
    }

    fn calls(&self) -> std::sync::MutexGuard<'_, HashMap<String, Arc<StateMachine>>> {
        lock(&self.calls)
    }

    /// `StartRinging`: a second ring on a device with a call in progress is `DeviceBusy`.
    pub fn start_ringing(
        &self,
        device_id: &str,
        call_id: &str,
    ) -> Result<Arc<StateMachine>, ManagerError> {
        let mut g = self.calls();
        if g.contains_key(device_id) {
            return Err(ManagerError::DeviceBusy);
        }
        let sm = Arc::new(StateMachine::new(call_id, device_id, State::Ringing));
        g.insert(device_id.to_string(), sm.clone());
        Ok(sm)
    }

    /// `Claim`: Ringing -> Answered; a wrong call id is `DeviceBusy`, a second claim
    /// `AlreadyAnswered`.
    pub fn claim(&self, device_id: &str, call_id: &str) -> Result<(), ManagerError> {
        let g = self.calls();
        let sm = g
            .get(device_id)
            .filter(|s| s.call_id == call_id)
            .ok_or(ManagerError::DeviceBusy)?;
        if sm.state() != State::Ringing {
            return Err(ManagerError::AlreadyAnswered);
        }
        sm.set_state(State::Answered);
        Ok(())
    }

    /// `Activate`: the claimed call's media is flowing.
    pub fn activate(&self, device_id: &str, call_id: &str) -> Result<(), ManagerError> {
        let g = self.calls();
        let sm = g
            .get(device_id)
            .filter(|s| s.call_id == call_id)
            .ok_or(ManagerError::DeviceBusy)?;
        sm.set_state(State::Active);
        Ok(())
    }

    /// `Revert`: a pre-media release puts the call back to Ringing.
    pub fn revert(&self, device_id: &str, call_id: &str) {
        if let Some(sm) = self.calls().get(device_id).filter(|s| s.call_id == call_id) {
            sm.set_state(State::Ringing);
        }
    }

    /// `End`: frees the device for a new ring.
    pub fn end(&self, device_id: &str, call_id: &str) {
        let mut g = self.calls();
        if g.get(device_id).is_some_and(|s| s.call_id == call_id) {
            g.remove(device_id);
        }
    }

    /// `Active`: the current call of a device, if any.
    pub fn active(&self, device_id: &str) -> Option<Arc<StateMachine>> {
        self.calls().get(device_id).cloned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // TestManager_SecondRingOnSameDeviceIsBusy
    #[test]
    fn second_ring_on_same_device_is_busy() {
        let m = Manager::new();
        assert!(m.start_ringing("device-1", "call-1").is_ok());
        assert_eq!(
            m.start_ringing("device-1", "call-2").err(),
            Some(ManagerError::DeviceBusy)
        );
        // A different device is unaffected.
        assert!(m.start_ringing("device-2", "call-3").is_ok());
    }

    // TestManager_ClaimRace_SecondClaimerGetsAlreadyAnswered
    #[test]
    fn claim_race_second_claimer_gets_already_answered() {
        let m = Manager::new();
        m.start_ringing("device-1", "call-1").unwrap();
        assert_eq!(m.claim("device-1", "call-1"), Ok(()));
        assert_eq!(
            m.claim("device-1", "call-1"),
            Err(ManagerError::AlreadyAnswered)
        );
    }

    // TestManager_ClaimWrongCallIDIsDeviceBusy
    #[test]
    fn claim_wrong_call_id_is_device_busy() {
        let m = Manager::new();
        m.start_ringing("device-1", "call-1").unwrap();
        assert_eq!(
            m.claim("device-1", "wrong-call-id"),
            Err(ManagerError::DeviceBusy)
        );
    }

    // TestManager_RevertFreesTheCallForAnotherClaimer
    #[test]
    fn revert_frees_the_call_for_another_claimer() {
        let m = Manager::new();
        m.start_ringing("device-1", "call-1").unwrap();
        m.claim("device-1", "call-1").unwrap();
        m.revert("device-1", "call-1");
        let sm = m
            .active("device-1")
            .expect("the call still exists as Ringing");
        assert_eq!(sm.state(), State::Ringing);
        assert_eq!(m.claim("device-1", "call-1"), Ok(()));
        m.activate("device-1", "call-1").unwrap();
        assert_eq!(m.active("device-1").unwrap().state(), State::Active);
    }

    // TestManager_EndFreesTheDeviceForANewRing
    #[test]
    fn end_frees_the_device_for_a_new_ring() {
        let m = Manager::new();
        m.start_ringing("device-1", "call-1").unwrap();
        m.end("device-1", "call-1");
        assert!(m.active("device-1").is_none());
        assert!(m.start_ringing("device-1", "call-2").is_ok());
        // a stale End (another call id) leaves the live one alone
        m.end("device-1", "call-1");
        assert!(m.active("device-1").is_some());
    }
}
