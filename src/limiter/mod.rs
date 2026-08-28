//! Rate limiter that decides whether a new OSC message should actually be
//! sent, to avoid tripping VRChat's chatbox anti-spam throttle.
//!
//! Rule: always allow sending when the *content* changed since the last send
//! (e.g. a new track started, or the text differs). If the content is the
//! same as last time, only allow sending again after `cooldown` has elapsed.

use std::time::{Duration, Instant};

pub struct RateLimiter {
    cooldown: Duration,
    last_sent_at: Option<Instant>,
    last_text: Option<String>,
}

impl RateLimiter {
    pub fn new(cooldown: Duration) -> Self {
        Self {
            cooldown,
            last_sent_at: None,
            last_text: None,
        }
    }

    /// Returns true if `text` should be sent right now, and if so, records it
    /// as the new "last sent" state. Uses `now` so it's fully testable without
    /// real sleeps.
    pub fn should_send_at(&mut self, text: &str, now: Instant) -> bool {
        let content_changed = self.last_text.as_deref() != Some(text);

        let allowed = match self.last_sent_at {
            None => true,
            Some(last) => content_changed || now.duration_since(last) >= self.cooldown,
        };

        if allowed {
            self.last_sent_at = Some(now);
            self.last_text = Some(text.to_string());
        }

        allowed
    }

    /// Convenience wrapper using the real system clock.
    pub fn should_send(&mut self, text: &str) -> bool {
        self.should_send_at(text, Instant::now())
    }

    /// Forces the next call to `should_send` to succeed regardless of cooldown,
    /// e.g. right after startup or a config reload.
    #[allow(dead_code)]
    pub fn reset(&mut self) {
        self.last_sent_at = None;
        self.last_text = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_send_always_allowed() {
        let mut rl = RateLimiter::new(Duration::from_secs(5));
        let now = Instant::now();
        assert!(rl.should_send_at("hello", now));
    }

    #[test]
    fn same_text_blocked_within_cooldown() {
        let mut rl = RateLimiter::new(Duration::from_secs(5));
        let t0 = Instant::now();
        assert!(rl.should_send_at("Artist - Song", t0));
        // 2 seconds later, same text, still under 5s cooldown -> blocked
        let t1 = t0 + Duration::from_secs(2);
        assert!(!rl.should_send_at("Artist - Song", t1));
    }

    #[test]
    fn same_text_allowed_after_cooldown() {
        let mut rl = RateLimiter::new(Duration::from_secs(5));
        let t0 = Instant::now();
        assert!(rl.should_send_at("Artist - Song", t0));
        let t1 = t0 + Duration::from_secs(6);
        assert!(rl.should_send_at("Artist - Song", t1));
    }

    #[test]
    fn track_change_bypasses_cooldown() {
        let mut rl = RateLimiter::new(Duration::from_secs(5));
        let t0 = Instant::now();
        assert!(rl.should_send_at("Artist - Song A", t0));
        // Immediately after, but different text (new track) -> allowed
        let t1 = t0 + Duration::from_millis(200);
        assert!(rl.should_send_at("Artist - Song B", t1));
    }

    #[test]
    fn repeated_identical_calls_only_first_passes() {
        let mut rl = RateLimiter::new(Duration::from_secs(3));
        let t0 = Instant::now();
        assert!(rl.should_send_at("X", t0));
        assert!(!rl.should_send_at("X", t0 + Duration::from_millis(500)));
        assert!(!rl.should_send_at("X", t0 + Duration::from_millis(999)));
        assert!(rl.should_send_at("X", t0 + Duration::from_secs(3)));
    }

    #[test]
    fn reset_clears_state() {
        let mut rl = RateLimiter::new(Duration::from_secs(5));
        let t0 = Instant::now();
        assert!(rl.should_send_at("X", t0));
        rl.reset();
        // Right away, should be allowed again because state was cleared.
        assert!(rl.should_send_at("X", t0 + Duration::from_millis(10)));
    }
}
