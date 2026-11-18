use thiserror::Error;

#[derive(Debug, Error)]
pub enum KilnError {
    #[error("unknown cone `{0}`")]
    UnknownCone(String),

    #[error("schedule has no segments")]
    EmptySchedule,

    #[error("io: {0}")]
    Io(#[from] std::io::Error),
}
