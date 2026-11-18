.PHONY: up down db-migrate db-reset seed

# Local dependencies (Postgres 16 + Redis 7) for the API, worker and integration tests.
up:
	docker compose up -d postgres redis

down:
	docker compose down

db-migrate:
	pnpm --filter @tallyhook/shared exec prisma migrate deploy

db-reset:
	pnpm --filter @tallyhook/shared exec prisma migrate reset --force

seed:
	pnpm --filter @tallyhook/shared exec tsx prisma/seed.ts
