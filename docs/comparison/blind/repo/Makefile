PY ?= python3

.PHONY: test test-all lint schema check-generated db-up db-down migrate backfill

# Unit tests only. Resampling tests assume UTC.
test:
	TZ=UTC $(PY) -m pytest -q -m "not integration" tests

# Full suite: starts TimescaleDB, loads fixtures for 14 stations, runs the
# integration tests. Takes 5-7 minutes on a laptop. CI runs this nightly.
test-all: db-up
	TZ=UTC $(PY) -m pytest -q tests --timescale-url=postgresql://tide:tide@localhost:5433/tide

lint: check-generated
	ruff check tidewater tests tools

schema:
	$(PY) tools/codegen.py schema/readings.avsc > tidewater/models/generated.py

check-generated:
	@$(PY) tools/codegen.py schema/readings.avsc | diff -q - tidewater/models/generated.py >/dev/null \
	  || (echo "tidewater/models/generated.py is stale or hand-edited; run 'make schema'" && exit 1)

db-up:
	docker compose up -d timescale
	@./tools/wait_for_db.sh

db-down:
	docker compose down

migrate:
	$(PY) -m tidewater.cli migrate --dir migrations

backfill:
	$(PY) -m tidewater.cli backfill --config config/pipeline.toml
