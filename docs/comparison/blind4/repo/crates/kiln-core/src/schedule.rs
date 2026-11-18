use chrono::{DateTime, Duration, TimeZone};
use serde::{Deserialize, Serialize};

use crate::error::KilnError;

/// One controller segment: ramp at `rate_c_per_h` until `target_c`, then hold.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Segment {
    /// °C per hour. Negative for controlled cooling.
    pub rate_c_per_h: i32,
    pub target_c: u16,
    pub hold_min: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Schedule {
    pub name: String,
    pub start_c: u16,
    pub segments: Vec<Segment>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Preset {
    FastBisque,
    SlowBisque,
    Glaze6,
}

fn seg(rate_c_per_h: i32, target_c: u16, hold_min: u32) -> Segment {
    Segment { rate_c_per_h, target_c, hold_min }
}

impl Schedule {
    pub fn preset(p: Preset) -> Schedule {
        let (name, segments) = match p {
            Preset::FastBisque => (
                "fast bisque (cone 04)",
                vec![seg(100, 120, 0), seg(200, 600, 0), seg(150, 1000, 10)],
            ),
            Preset::SlowBisque => (
                "slow bisque (cone 04)",
                vec![seg(50, 120, 60), seg(100, 600, 0), seg(80, 1000, 15)],
            ),
            Preset::Glaze6 => (
                "glaze (cone 6)",
                vec![seg(80, 200, 0), seg(180, 1100, 0), seg(100, 1222, 15)],
            ),
        };
        Schedule { name: name.to_string(), start_c: 20, segments }
    }

    /// Replace the peak of the hottest segment, e.g. to fire a preset to another cone.
    pub fn with_peak(mut self, peak_c: u16) -> Schedule {
        if let Some(top) = self.segments.iter_mut().max_by_key(|s| s.target_c) {
            top.target_c = peak_c;
        }
        self
    }

    pub fn validate(&self) -> Result<(), KilnError> {
        if self.segments.is_empty() {
            return Err(KilnError::EmptySchedule);
        }
        Ok(())
    }

    /// Ramp + hold time. Natural cooling after the last segment is not included.
    pub fn duration(&self) -> Duration {
        let mut total = Duration::zero();
        let mut temp = i64::from(self.start_c);
        for s in &self.segments {
            let delta = (i64::from(s.target_c) - temp).abs();
            let rate = i64::from(s.rate_c_per_h).abs().max(1);
            total = total + Duration::minutes(delta * 60 / rate) + Duration::minutes(i64::from(s.hold_min));
            temp = i64::from(s.target_c);
        }
        total
    }

    pub fn eta_from<Tz: TimeZone>(&self, start: DateTime<Tz>) -> DateTime<Tz> {
        start + self.duration()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Local;

    #[test]
    fn fast_bisque_duration() {
        // 100 °C @100/h + 480 °C @200/h + 400 °C @150/h + 10 min hold
        assert_eq!(Schedule::preset(Preset::FastBisque).duration(), Duration::minutes(374));
    }

    #[test]
    fn empty_schedule_is_invalid() {
        let s = Schedule { name: "x".into(), start_c: 20, segments: vec![] };
        assert!(matches!(s.validate(), Err(KilnError::EmptySchedule)));
    }

    #[test]
    fn with_peak_raises_top_segment() {
        let s = Schedule::preset(Preset::Glaze6).with_peak(1305);
        assert_eq!(s.segments.last().map(|s| s.target_c), Some(1305));
    }

    #[test]
    fn eta_same_day_for_short_bisque() {
        // a fast bisque is ~6h, so it should finish the same day it starts
        let start = Local::now();
        let eta = Schedule::preset(Preset::FastBisque).eta_from(start);
        assert_eq!(eta.date_naive(), start.date_naive());
    }
}
