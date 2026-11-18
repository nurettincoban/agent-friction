#!/usr/bin/env bash
# Throw away the local database volume and rebuild it from migrations + seed.
set -euo pipefail
docker compose down -v
docker compose up -d db
sleep 3
migrate -path migrations -database "${FERRYDESK_DATABASE_URL:-postgres://ferry:ferry@localhost:5432/ferrydesk?sslmode=disable}" up
psql "${FERRYDESK_DATABASE_URL:-postgres://ferry:ferry@localhost:5432/ferrydesk?sslmode=disable}" -f scripts/seed.sql
