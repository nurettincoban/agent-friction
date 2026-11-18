//! kiln-core: Orton cone table, firing schedules and unit conversion.
//!
//! Everything in here is plain data + pure functions so it can be shared by
//! `kilnctl` and `kiln-hub`.

pub mod cones;
pub mod error;
pub mod schedule;
pub mod units;

pub use cones::{cone_to_celsius, Cone, CONES};
pub use error::KilnError;
pub use schedule::{Preset, Schedule, Segment};
