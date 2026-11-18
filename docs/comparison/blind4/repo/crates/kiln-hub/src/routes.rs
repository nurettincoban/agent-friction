use axum::{
    extract::{Path, State},
    http::StatusCode,
    routing::get,
    Json, Router,
};
use serde::Deserialize;
use sqlx::SqlitePool;

use crate::db::Firing;

pub fn router(pool: SqlitePool) -> Router {
    Router::new()
        .route("/health", get(|| async { "ok" }))
        .route("/cones/{name}", get(get_cone))
        .route("/firings", get(list_firings).post(create_firing))
        .route("/firings/{id}", get(get_firing))
        .with_state(pool)
}

type ApiResult<T> = Result<Json<T>, (StatusCode, String)>;

fn internal(e: impl std::fmt::Display) -> (StatusCode, String) {
    (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
}

async fn get_cone(Path(name): Path<String>) -> ApiResult<serde_json::Value> {
    let peak = kiln_core::cone_to_celsius(&name)
        .map_err(|e| (StatusCode::NOT_FOUND, e.to_string()))?;
    Ok(Json(serde_json::json!({ "cone": name, "peak_c": peak })))
}

async fn list_firings(State(pool): State<SqlitePool>) -> ApiResult<Vec<Firing>> {
    let rows = sqlx::query_as!(
        Firing,
        "SELECT id, kiln_id, preset, peak_c, started_at, finished_at FROM firings ORDER BY started_at DESC LIMIT 100"
    )
    .fetch_all(&pool)
    .await
    .map_err(internal)?;
    Ok(Json(rows))
}

#[derive(Debug, Deserialize)]
pub struct NewFiring {
    pub kiln_id: i64,
    pub preset: String,
    pub peak_c: i64,
}

async fn create_firing(
    State(pool): State<SqlitePool>,
    Json(body): Json<NewFiring>,
) -> ApiResult<Firing> {
    let row = sqlx::query_as!(
        Firing,
        "INSERT INTO firings (kiln_id, preset, peak_c, started_at) VALUES (?, ?, ?, datetime('now')) RETURNING id, kiln_id, preset, peak_c, started_at, finished_at",
        body.kiln_id,
        body.preset,
        body.peak_c
    )
    .fetch_one(&pool)
    .await
    .map_err(internal)?;
    Ok(Json(row))
}

async fn get_firing(State(pool): State<SqlitePool>, Path(id): Path<i64>) -> ApiResult<Firing> {
    let row = sqlx::query_as!(
        Firing,
        "SELECT id, kiln_id, preset, peak_c, started_at, finished_at FROM firings WHERE id = ?",
        id
    )
    .fetch_optional(&pool)
    .await
    .map_err(internal)?
    .ok_or((StatusCode::NOT_FOUND, format!("firing {id} not found")))?;
    Ok(Json(row))
}
