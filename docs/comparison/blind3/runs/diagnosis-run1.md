# Diagnosis: tallyhook

From `.agent-friction/evidence.md` (16 sessions, S01–S16, 2026-10-06 to 2026-11-13), checked
against the repository at commit `2e188d8`. `node_modules` is not installed in this checkout,
so no command was re-run; mechanisms were confirmed by reading the files named below.

## 1. Integration tests and dev servers are started without Postgres and Redis running

**Seen in:** S04, S11, S15, S16 (4 of 16 sessions) · `pnpm test:int` failed in S04, S11 and S16; the human had to say `make up` in S04, S15 and S16
**What happens:** `packages/api/test/requeue.int.test.ts:6` runs against the real Postgres and Redis from `docker-compose.yml`. Nothing starts them: `test:int` is a bare `vitest run -c vitest.int.config.ts` (`packages/api/package.json:11`). The only way up is `make up` (`Makefile:4-5`), followed by `make db-migrate` (`Makefile:10-11`). Agents instead probe with `redis-cli` (not installed, S04) or try `brew services start redis` (S16).
**Evidence:**
- `[ioredis] Unhandled error event: Error: connect ECONNREFUSED 127.0.0.1:6379` and `` Can't reach database server at `localhost:5432` `` (S04, S11, S16)
- S04: "redis isn't up. make up"
- S15: "yes. `make up` first - fyi you need make up before any of the int/dev stuff, I keep saying this"
- S16: "make up. it's docker compose, not brew. and honestly can you put that in CLAUDE.md so we stop going through this every single time"
**Why it keeps happening:** Not written down. `CLAUDE.md:13` says the integration tests "need Postgres + Redis" but not how to get them, and the Makefile is not mentioned anywhere in `CLAUDE.md`. The human asked for this line in S16; it is still missing.
**Fix:**
- `Makefile`: add a target that brings the services up first, and make `up` wait for them:
  ```make
  .PHONY: up down db-migrate db-reset seed test-int

  up:
  	docker compose up -d --wait postgres redis

  test-int: up db-migrate
  	pnpm --filter @tallyhook/api test:int
  ```
- `docker-compose.yml`: give `--wait` something to wait for:
  ```yaml
    postgres:
      healthcheck:
        test: ['CMD-SHELL', 'pg_isready -U tallyhook']
        interval: 2s
        retries: 15
    redis:
      healthcheck:
        test: ['CMD', 'redis-cli', 'ping']
        interval: 2s
        retries: 15
  ```
- `CLAUDE.md`, under Commands, replace the `test:int` line with:
  ```markdown
  - `make up` - start Postgres + Redis (docker compose). Required before integration tests and
    before `pnpm dev` in api or worker. Never install or start them another way (no brew, no
    local redis-cli/psql).
  - `make test-int` - `make up`, apply migrations, then run the API integration tests
  ```
**Confidence:** high. Same error lines in all three failures; in S04 and S16 the tests ran after `make up`.

## 2. Agents log with `console.*`, which the log shipper drops

**Seen in:** S06, S08, S14 (3 of 16 sessions) · corrected by the human each time
**What happens:** The repository's logger is pino, JSON on stdout (`packages/shared/src/logger.ts:3-7`), and Fastify is built with it (`packages/api/src/server.ts:8`), so `req.log` is the same logger. New code written by agents used `console.log` (S06, S08) and `console.warn` (S14).
**Evidence:**
- S06: "thanks, but swap that console.log for the logger from shared pls - anything on stdout that isn't JSON gets dropped by the log shipper, so that line would just vanish in prod"
- S08: "no need to run it. and logger, not console.log. same as last time"
- S14: "srsly, console.warn is still console. req.log.warn (or the shared logger). can you also grep for any other console.* in api + worker src whlie you're at it"
**Why it keeps happening:** Not written down and not enforced. `CLAUDE.md` lists `logger` as part of `packages/shared` (line 7) but has no rule about logging, and `.eslintrc.cjs:16-19` has no `no-console` rule (the agent's own search for `no-console` in S14 found nothing). The tree currently has no `console.` call, so the rule can be switched on without other changes.
**Fix:**
- `.eslintrc.cjs`, in `rules`:
  ```js
      'no-console': 'error',
  ```
- `CLAUDE.md`, under Conventions:
  ```markdown
  - Never use `console.*`. Log with `logger` from `@tallyhook/shared` (in API routes: `req.log`).
    The log shipper drops any stdout line that is not JSON.
  ```
**Confidence:** high.

## 3. Changes outside what was asked: formatting churn, renames, refactors

**Seen in:** S02, S06, S08 (3 of 16 sessions) · reverted on the human's request each time
**What happens:** In S02 the agent ran `pnpm exec prettier --write packages/`, which reformatted files it had not touched. The tree is not prettier-clean, so any wide `prettier --write` produces unrelated diffs: `packages/api/test/requeue.int.test.ts:19` and `:23` are 101 columns against `printWidth: 100` (`.prettierrc:4`), and the root `format` script is `prettier --write .` (`package.json:10`). In S06 the agent renamed `postWebhook` while adding a timeout; in S08 it refactored `recordFailure` while adding metrics.
**Evidence:**
- S02: "uh why does git status show like 9 changed files? i asked for the disable endpoint. revert the prettier churn pls, keep the diff to what i asked for"
- S06: "oh wait, why is postWebhook renamed?? i only asked for the timeout. undo that pls, dana has an open PR touching deliver.ts and this will conflict for no reason"
- S08: "pls revert the recordFailure refactor, it's unrelated to metrics and makes the diff harder to review. just add the record() calls inline"
**Why it keeps happening:** Not written down. `CLAUDE.md:25` states the prettier settings, which reads as an invitation to run prettier, and nothing says to keep the diff to the request.
**Fix:**
- `CLAUDE.md`, under Conventions, replace the Prettier line with:
  ```markdown
  - Keep the diff to what was asked: no renames, refactors or reformatting of code the task does
    not need. Other people have open PRs on these files.
  - Prettier: single quotes, trailing commas, 100 cols. Format only the files you changed
    (`pnpm exec prettier --write <files>`); never `pnpm format` or `prettier --write` on a
    directory.
  ```
- Optional, removes the churn at the source: one commit that only runs `pnpm format`, so a wide prettier run changes nothing afterwards.
**Confidence:** high for the rule (three human turns). Medium for the prettier mechanism: two over-long lines confirmed by measuring; prettier itself was not run.

## 4. `pnpm test` fails at night on a test that reads the wall clock

**Seen in:** S04, S12, S13 (3 of 16 sessions) · `pnpm test` exit 1, same test each time, in sessions that changed nothing in it
**What happens:** `packages/worker/src/quietHours.test.ts:17` calls `isQuietHours(DEFAULT_QUIET_WINDOW)` with no date, so `quietHours.ts:10` uses `new Date()`. The default window is 22:00–07:00 (`quietHours.ts:8`), so the assertion `toBe(false)` fails whenever the tests run in that window. The three failing sessions started at 22:35, 22:48 and 06:21 UTC; no session outside the window has the failure.
**Evidence:** `packages/worker test: × isQuietHours > does not hold deliveries for a freshly created endpoint` / `→ expected true to be false // Object.is equality` (S04, S12, S13)
**Why it keeps happening:** The test depends on the time of day. An agent that sees it fail cannot tell it from a regression it caused, and `CLAUDE.md:17` requires `pnpm test` to pass before a change is called done.
**Fix:**
- `packages/worker/src/quietHours.test.ts:17`:
  ```diff
  -    expect(isQuietHours(DEFAULT_QUIET_WINDOW)).toBe(false);
  +    expect(isQuietHours(DEFAULT_QUIET_WINDOW, new Date('2026-01-01T12:00:00'))).toBe(false);
  ```
- For a human to decide, not part of this fix: the test's name says a new endpoint should deliver right away, yet the default window holds deliveries at night. If that is a product bug, it is in `DEFAULT_QUIET_WINDOW`, not in the test.
**Confidence:** high. The mechanism is in the code and the three start times all fall inside the window.

## 5. Agents edit Prisma migrations that are already deployed

**Seen in:** S05, S10 (2 of 16 sessions) · declined by the human in S05, corrected after the edit in S10
**What happens:** To add a column or index, the agent edits an existing `packages/shared/prisma/migrations/*/migration.sql` instead of adding a new migration directory. Prisma stores a checksum per applied migration, so `prisma migrate deploy` (`Makefile:10-11`) fails on any environment where the edited one has run.
**Evidence:**
- S05: "don't touch that one, it's already been applied on staging and prod. new migration please"
- S10: "again with editing old migrations?? 20261015112400 is deployed, prisma will fail the checksum on staging. new migration. every. time."
**Why it keeps happening:** Not written down and not enforced. `CLAUDE.md` does not mention migrations, and no `.claude/settings.json` exists. Said twice, the second time with "again", so a line alone is not enough.
**Fix:**
- `CLAUDE.md`, under Conventions:
  ```markdown
  - Never edit an existing file under `packages/shared/prisma/migrations/`: they are applied on
    staging and prod and Prisma checks their checksums. For any schema change, edit
    `schema.prisma` and add a new migration directory (`<UTC yyyymmddHHMMSS>_<name>/migration.sql`).
  ```
- `.claude/hooks/no-committed-migration-edits.sh` (new), which blocks edits to migrations that are already committed and lets new ones through:
  ```sh
  #!/bin/sh
  # Block edits to Prisma migrations that are already committed: they may be applied on staging/prod.
  f=$(node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>console.log(JSON.parse(s).tool_input?.file_path??""))')
  case "$f" in
    */packages/shared/prisma/migrations/*/migration.sql)
      rel=${f#"$CLAUDE_PROJECT_DIR"/}
      if git -C "$CLAUDE_PROJECT_DIR" cat-file -e "HEAD:$rel" 2>/dev/null; then
        echo "This migration is already committed and may be applied on staging/prod. Add a new migration directory instead." >&2
        exit 2
      fi ;;
  esac
  exit 0
  ```
- `.claude/settings.json` (new):
  ```json
  {
    "hooks": {
      "PreToolUse": [
        {
          "matcher": "Edit|Write",
          "hooks": [
            {
              "type": "command",
              "command": "sh \"$CLAUDE_PROJECT_DIR/.claude/hooks/no-committed-migration-edits.sh\""
            }
          ]
        }
      ]
    }
  }
  ```
**Confidence:** high for the cause. The hook script was run by hand against this tree (blocks `20261002120000_add_endpoint_secret/migration.sql`, allows a new directory); it was not run inside a Claude Code session.

## 6. Work reported as done that had not actually run

**Seen in:** S09 (reported by the human at the start of S10), S15 (2 of 16 sessions)
**What happens:** In S09 the agent ran the worker tests with a filter that matched no file. The worker's test script is `vitest run --passWithNoTests` (`packages/worker/package.json:10`), so vitest printed "No test files found" and exited 0, and the agent reported the tests as passing; the real `signature.test.ts` was red on main. In S15 the agent reported `/readyz` done on unit tests alone, never having called the endpoint.
**Evidence:**
- S10 (opening request): "CI on main is red: signature.test.ts > signPayload > is a hex HMAC-SHA256 of the body. you told me tuesday night the signer tests passed - did you actually run them? there is no file called signer, vitest just printed 'No test files found' and exited 0"
- S15: "did you actually hit it though? redis isn't even running on my machine right now so a 200 would be wrong"
**Why it keeps happening:** The default command hides the mistake: `--passWithNoTests` turns "ran nothing" into a green exit. The worker has four test files, so the flag protects nothing. The api and shared packages do not have it.
**Fix:**
- `packages/worker/package.json:10`:
  ```diff
  -    "test": "vitest run --passWithNoTests"
  +    "test": "vitest run"
  ```
- `CLAUDE.md`, after the "Run `pnpm typecheck && pnpm test`" line:
  ```markdown
  When you report tests as passing, check that the output lists the test files you meant to run.
  For a new or changed HTTP route that talks to Postgres or Redis, also call it once against
  `make up` services, or say plainly that you did not.
  ```
**Confidence:** high for S09 (flag confirmed in the file, matches the human's description). Medium for S15: one human turn, and the fix there is a habit, not a mechanism.

## 7. `pnpm typecheck` fails until the Prisma client is generated

**Seen in:** S01, S02 (2 of 16 sessions) · `pnpm typecheck` exit 2 in `@tallyhook/shared`, passed after `pnpm --filter @tallyhook/shared db:generate` with no file edited
**What happens:** `packages/shared/src/db.ts:1` imports `./generated/prisma`. That directory is the Prisma client output (`packages/shared/prisma/schema.prisma:3`) and is git-ignored (`.gitignore:5`). Nothing generates it on install: no package has a `postinstall` or `prepare` script. The same step is needed again after every edit to `schema.prisma`.
**Evidence:** `ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL  @tallyhook/shared@0.1.0 typecheck: `tsc --noEmit -p .`` / `Exit status 2` (S01, S02)
**Why it keeps happening:** Not written down and not automated. `CLAUDE.md:10-15` lists the commands without `db:generate`.
**Fix:**
- `packages/shared/package.json`, in `scripts`:
  ```json
      "postinstall": "prisma generate",
  ```
- `CLAUDE.md`, under Commands:
  ```markdown
  - `pnpm --filter @tallyhook/shared db:generate` - regenerate the Prisma client. Run it after
    every change to `schema.prisma`, before `pnpm typecheck`.
  ```
**Confidence:** medium. The mechanism is confirmed in the files; the full tsc output is cut in the evidence, so the exact error line was not seen.

## 8. `pnpm lint` runs past the 2-minute command timeout

**Seen in:** S03, S07 (2 of 16 sessions) · `Command timed out after 2m 0.0s` both times
**What happens:** `pnpm lint` is `eslint . --ext .ts --max-warnings 0` (`package.json:9`) with type-aware rules over every tsconfig in the workspace (`.eslintrc.cjs:7`, `:13`), in one process. Both requests needed lint to pass (S03 "the lint job on main is red", S07 "make sure everything still passes incl lint").
**Evidence:** `Command timed out after 2m 0.0s` (S03, S07)
**Why it keeps happening:** Unconfirmed. Type-aware linting of the whole workspace is slow by design, but how long it takes here could not be measured (no `node_modules` in this checkout). `CLAUDE.md:15` lists the command with no hint of its duration.
**Fix:**
- Measure first: `time pnpm lint` on a warm checkout. If it is well over two minutes, the project glob at `.eslintrc.cjs:7` (`./**/tsconfig.json`) is the first thing to narrow to `./packages/*/tsconfig.json`.
- `CLAUDE.md`, replace the `pnpm lint` line with:
  ```markdown
  - `pnpm lint` - type-aware eslint over the whole workspace; takes longer than 2 minutes, so run
    it with a 5-minute timeout. While iterating, lint one package:
    `pnpm exec eslint packages/worker --ext .ts --max-warnings 0`
  ```
**Confidence:** unconfirmed mechanism; the timeouts themselves are in the evidence.

## 9. Commands that never exit are run in the foreground

**Seen in:** S08, S16 (2 of 16 sessions) · interrupted by the human both times
**What happens:** `pnpm --filter @tallyhook/worker dev` is `tsx watch src/index.ts` (`packages/worker/package.json:7`) and `docker compose logs -f redis` follows the log; neither returns.
**Evidence:**
- S08: "no need to run it. and logger, not console.log. same as last time"
- S16: "-f never exits lol. it's up, just run the tests"
**Why it keeps happening:** Not written down.
**Fix:**
- `CLAUDE.md`, under Commands:
  ```markdown
  - `pnpm --filter @tallyhook/api dev` / `pnpm --filter @tallyhook/worker dev` are watch-mode
    servers that never exit: run them in the background and stop them when done. Never use
    `docker compose logs -f`; use `docker compose logs --tail 50 <service>`.
  ```
**Confidence:** medium. Two interruptions, but in S08 the human's point was also that running the worker was not needed at all.

## Looked at, not a problem

- S01 "can you make the default limit 25 instead of 50?": a follow-up request.
- S06 "also add the env var to .env.example": a follow-up request; the `.env.example` "probable correction" is the `postWebhook` rename, counted under problem 3.
- S10 "we need an index on Delivery(endpointId, status)": a new task.
- S14 "actually hold on - keep it at 3": product changed its mind; the reverts in `schema.prisma` and `retry.ts` follow from that.
- S04 "ok cool thx": thanks. The `pnpm test` failure just before it is problem 4.
- S14 searches for `console.` in `packages`, `packages/api/src`, `packages/worker/src`: the human asked for that grep; finding nothing was the good outcome.
- S11 search for `lastDeliveredAt` in `schema.prisma`, S01 search for `listDeliveriesQuery|delivery\.findMany`: looking for something before adding it.
- S07 edit miss in `packages/shared/package.json`: the agent searched for `"zod": "^3.22.4"`, the file pins `"zod": "3.22.4"` (line 16). One-off.
- S12 `pnpm install` ECONNRESET from the npm registry: transient network failure.
- S04 `redis-cli: command not found`: a symptom of problem 1.
- `packages/worker/src/metrics.ts` and `metrics.test.ts` "not in tree": the S08 interruption, counted under problem 9.

## Measure it

After the fixes, run `/agent-friction:compare` in a week or two to see whether these failures went down.
