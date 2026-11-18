# ferrydesk

Booking backend for Tidewater ferry routes: sailings, seat holds, bookings,
fares and refunds. HTTP JSON API in Go, Postgres for storage.

## Quick start

```sh
docker compose up -d db
make migrate-up
make seed
make run          # serves the API on :8080
```

## Configuration

All configuration comes from environment variables:

| Variable            | Default                                                        | Notes                     |
|---------------------|----------------------------------------------------------------|---------------------------|
| `FD_PORT`           | `8080`                                                         | HTTP listen port          |
| `FD_DATABASE_URL`   | `postgres://ferry:ferry@localhost:5432/ferrydesk?sslmode=disable` |                        |
| `FD_LOG_LEVEL`      | `info`                                                         | debug, info, warn, error  |
| `FD_HOLD_TTL`       | `15m`                                                          | how long a seat hold lasts |

Copy `.env.example` to `.env` and adjust.

## Layout

- `cmd/ferrydesk` – entrypoint
- `internal/api` – HTTP handlers and routing
- `internal/booking` – holds, bookings, cancellation rules
- `internal/pricing` – fares and discounts
- `internal/store` – Postgres implementation of the booking store
- `internal/db` – sqlc output (generated from `db/query/*.sql`, run `make generate`)
- `migrations` – golang-migrate SQL migrations

## Tests

```sh
make test
```

## Lint

```sh
make lint
```
