mod db;
mod routes;

use std::net::SocketAddr;

use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    dotenvy::dotenv().ok();
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .init();

    let pool = db::connect().await?;
    sqlx::migrate!("./migrations").run(&pool).await?;

    let addr: SocketAddr = std::env::var("KILNHUB_BIND")
        .unwrap_or_else(|_| "127.0.0.1:8087".into())
        .parse()?;
    let app = routes::router(pool);

    tracing::info!(%addr, "kiln-hub listening");
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;
    Ok(())
}
