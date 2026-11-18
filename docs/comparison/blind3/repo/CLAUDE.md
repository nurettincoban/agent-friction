# Tallyhook

Webhook delivery service. pnpm monorepo:

- `packages/api` - Fastify HTTP API (tenants register endpoints, inspect deliveries, requeue dead letters)
- `packages/worker` - BullMQ worker that signs and POSTs webhook payloads, with retry/backoff
- `packages/shared` - Prisma schema + client, zod request schemas, retry helpers, logger

## Commands

- `pnpm install`
- `pnpm typecheck` - tsc across all packages
- `pnpm test` - unit tests (vitest) across all packages
- `pnpm --filter @tallyhook/api test:int` - integration tests (need Postgres + Redis)
- `pnpm lint`

Run `pnpm typecheck && pnpm test` before saying a change is done.

## Conventions

- TypeScript strict mode everywhere. No `any` unless there's a comment explaining why.
- Request validation goes through zod schemas in `packages/shared/src/schemas.ts`; routes call `.parse()`.
- API routes are Fastify plugins under `packages/api/src/routes/`, registered with the `/v1` prefix in `server.ts`.
- Error responses are `{ error: "<snake_case_code>" }`.
- Branch names: `feat/...`, `fix/...`, `chore/...`.
- Prettier: single quotes, trailing commas, 100 cols.
