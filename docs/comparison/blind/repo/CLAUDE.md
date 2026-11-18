# tidewater

Ingests tide-gauge readings (NOAA CO-OPS API + CSV drops from partner stations),
runs QC, resamples, and loads into TimescaleDB.

## Layout
- `tidewater/ingest/` – source adapters (NOAA client, CSV drop parser)
- `tidewater/transform/` – QC flags, resampling, unit helpers
- `tidewater/load/` – warehouse writer
- `tidewater/models/generated.py` – reading dataclasses

## Codegen
`tidewater/models/generated.py` is generated from `schema/readings.avsc` by
`tools/codegen.py`. Do not edit it by hand: change the schema and run
`make schema`. `make lint` fails if the generated file is stale.

## Commands
- `make test` – unit tests
- `make lint` – ruff + generated-file check
- `make db-up` – start local TimescaleDB (docker compose)
- `make migrate` – apply `migrations/*.sql` to the local DB
