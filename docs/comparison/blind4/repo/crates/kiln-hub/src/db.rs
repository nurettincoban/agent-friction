use std::str::FromStr;

use chrono::NaiveDateTime;
use serde::Serialize;
use sqlx::sqlite::{SqliteConnectOptions, SqlitePool, SqlitePoolOptions};

pub async fn connect() -> anyhow::Result<SqlitePool> {
    let url = std::env::var("DATABASE_URL")?;
    let opts = SqliteConnectOptions::from_str(&url)?.create_if_missing(true);
    Ok(SqlitePoolOptions::new()
        .max_connections(4)
        .connect_with(opts)
        .await?)
}

#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct Firing {
    pub id: i64,
    pub kiln_id: i64,
    pub preset: String,
    pub peak_c: i64,
    pub started_at: NaiveDateTime,
    pub finished_at: Option<NaiveDateTime>,
}
