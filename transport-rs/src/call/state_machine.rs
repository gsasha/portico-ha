//! `state_machine.go`: one instance per active call. States `Ringing -> Answered -> Active ->
//! Ended`, wire values `ringing|answered|active|ended` (`idle` is HA-side only).

use crate::util::lock;
use std::sync::Mutex;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Ringing,
    Answered,
    Active,
    Ended,
}

impl State {
    /// `State.String`.
    pub fn as_str(self) -> &'static str {
        match self {
            State::Ringing => "ringing",
            State::Answered => "answered",
            State::Active => "active",
            State::Ended => "ended",
        }
    }
}

impl std::fmt::Display for State {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One active call's state.
#[derive(Debug)]
pub struct StateMachine {
    pub call_id: String,
    pub device_id: String,
    state: Mutex<State>,
}

impl StateMachine {
    pub(crate) fn new(call_id: &str, device_id: &str, state: State) -> Self {
        StateMachine {
            call_id: call_id.to_string(),
            device_id: device_id.to_string(),
            state: Mutex::new(state),
        }
    }

    pub fn state(&self) -> State {
        *lock(&self.state)
    }

    pub(crate) fn set_state(&self, s: State) {
        *lock(&self.state) = s;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wire_values_are_the_four_state_names() {
        assert_eq!(State::Ringing.to_string(), "ringing");
        assert_eq!(State::Answered.to_string(), "answered");
        assert_eq!(State::Active.to_string(), "active");
        assert_eq!(State::Ended.to_string(), "ended");
    }
}
