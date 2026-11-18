# Diagnosis: tallyhook

From `.agent-friction/evidence.md` (16 sessions, 248 tool calls, 2026-10-06 to 2026-11-13),
checked against the repository at commit `71c7489`. `node_modules` is not installed in this
checkout, so no pnpm command was re-run here; mechanisms were confirmed by reading the files.

## 1. Agents do not know that `make up` starts Postgres and Redis

**Seen in:** S04, S11, S15, S16 (4 of 16 sessions) · `pnpm test:int` failed in S04, S11 and S16; the human said `make up` in S04, S15 and S16
**What happens:** `packages/api/test/requeue.int.test.ts:6` runs against the real Postgres and Redis from `docker-compose.yml`, which only `make up` starts (`Makefile:4-5`). With nothing running, the test hangs until the hook times out. Agents then guess: `redis-cli ping` (not installed, S04), `brew services start redis` (declined, S16), `docker compose logs -f redis` (never exits, interrupted, S16). In S04 the test passed only after `make db-migrate` as well.
**Evidence:**
- `[ioredis] Unhandled error event: Error: connect ECONNREFUSED 127.0.0.1:6379`, `` Can't reach database server at `localhost:5432` ``, `Error: Hook timed out in 10000ms.` (each in S04, S11, S16)
- S04: "redis isn't up. make up"
- S15: "yes. `make up` first - fyi you need make up before any of the int/dev stuff, I keep saying this"
- S16: "make up. it's docker compose, not brew. and honestly can you put that in CLAUDE.md so we stop going through this every single time"
- S16: "-f never exits lol. it's up, just run the tests"

**Why it keeps happening:** `CLAUDE.md:14` says the integration tests "need Postgres + Redis" but not how to get them. `make up` is written down only in the Makefile comment. The human asked for it to go into CLAUDE.md in S16; it is still not there.
**Fix:**
- `CLAUDE.md`, replace line 14 with:
  ```markdown
  - `make up` - start Postgres + Redis (docker compose). Required before `test:int`, `dev`, or
    anything else that talks to the database or queue. Never brew, never a local redis/postgres.
  - `make db-migrate` - apply migrations to the local database (after `make up`, and after adding a migration)
  - `pnpm --filter @tallyhook/api test:int` - integration tests (run `make up && make db-migrate` first)
  ```
- `CLAUDE.md`, under Conventions:
  ```markdown
  - Do not run commands that never exit in the foreground (`docker compose logs -f`, `pnpm dev`).
    Use `docker compose ps` / `docker compose logs --tail 50`, or run them in the background.
  ```
- Optional, removes the guessing: a `Makefile` target
  ```make
  test-int: up db-migrate
  	pnpm --filter @tallyhook/api test:int
  ```
  (add `test-int` to `.PHONY`). `docker-compose.yml` has no healthchecks, so `db-migrate` can race a cold Postgres start; `docker compose up -d --wait` needs healthchecks to help.

**Confidence:** high. Three sessions with the same error, three human statements, and CLAUDE.md does not name `make up`.

## 2. Agents log with `console.*`; the repository requires the shared logger

**Seen in:** S06, S08, S14 (3 of 16 sessions) · corrected by the human each time
**What happens:** New code used `console.log` (S06, S08) and `console.warn` (S14). `packages/shared/src/logger.ts:3-7` is a pino logger writing JSON to stdout, and the log shipper drops every line that is not JSON.
**Evidence:**
- S06: "thanks, but swap that console.log for the logger from shared pls - anything on stdout that isn't JSON gets dropped by the log shipper, so that line would just vanish in prod"
- S08: "no need to run it. and logger, not console.log. same as last time"
- S14: "srsly, console.warn is still console. req.log.warn (or the shared logger). can you also grep for any other console.* in api + worker src whlie you're at it"

**Why it keeps happening:** Not written down and not enforced. `CLAUDE.md` Conventions has no line on logging, and `.eslintrc.cjs:16-19` has no `no-console` rule (the agent's own search for it in S14 found nothing). There is no `console.` call in `packages/` today, so turning the rule on breaks nothing.
**Fix:**
- `.eslintrc.cjs`, in `rules`:
  ```js
  'no-console': 'error',
  ```
- `CLAUDE.md`, under Conventions:
  ```markdown
  - No `console.*` in api or worker code. Use `logger` from `@tallyhook/shared` (or `req.log` in
    routes): the log shipper drops any stdout line that is not JSON.
  ```

**Confidence:** high.

## 3. Agents widen the diff beyond what was asked

**Seen in:** S02, S06, S08 (3 of 16 sessions) · the human asked for a revert each time
**What happens:** S02: `pnpm exec prettier --write packages/` reformatted unrelated files. S06: `postWebhook` renamed during a timeout change. S08: `recordFailure` refactored during a metrics change.
**Evidence:**
- S02: "uh why does git status show like 9 changed files? i asked for the disable endpoint. revert the prettier churn pls, keep the diff to what i asked for"
- S06: "oh wait, why is postWebhook renamed?? i only asked for the timeout. undo that pls, dana has an open PR touching deliver.ts and this will conflict for no reason"
- S08: "pls revert the recordFailure refactor, it's unrelated to metrics and makes the diff harder to review. just add the record() calls inline"

**Why it keeps happening:** Not written down. For the prettier case there is also a cause in the tree: `prettier --check .` reports 3 committed files that are not formatted (`.eslintrc.cjs`, `packages/api/test/requeue.int.test.ts`, `packages/worker/src/index.ts`), and the root `format` script is `prettier --write .` (`package.json:9`), so any formatting run touches files outside the task.
**Fix:**
- `CLAUDE.md`, under Conventions:
  ```markdown
  - Keep the diff to what was asked. No drive-by renames or refactors (other PRs are open on the
    same files); propose them instead. Format only the files you changed:
    `pnpm exec prettier --write <files>`, never `pnpm format` or `prettier --write packages/`.
  ```
- Optional, one `chore/format` commit: `pnpm format` once, so a stray formatting run changes nothing.

**Confidence:** high for the convention; the prettier mechanism is confirmed on today's tree (3 files; S02 saw about 9).

## 4. `pnpm test` fails at night on a clock-dependent test

**Seen in:** S04, S12, S13 (3 of 16 sessions) · `pnpm test` failed each time, in work that had nothing to do with quiet hours
**What happens:** `packages/worker/src/quietHours.test.ts:15-18` calls `isQuietHours(DEFAULT_QUIET_WINDOW)` with no date, so it uses the current time, and expects `false`. The default window is 22:00 to 07:00 (`quietHours.ts:8`). The three sessions started at 22:35, 22:48 and 06:21 UTC: all inside the window.
**Evidence:** `packages/worker test: × isQuietHours > does not hold deliveries for a freshly created endpoint` and `→ expected true to be false // Object.is equality` (S04, S12, S13)
**Why it keeps happening:** The test depends on the wall clock, and `CLAUDE.md:17` makes `pnpm test` the gate for every change, so every night session meets a red suite it did not cause.
**Fix:**
- `packages/worker/src/quietHours.test.ts:17`, pin the time:
  ```ts
  expect(isQuietHours(DEFAULT_QUIET_WINDOW, new Date('2026-01-01T12:00:00'))).toBe(false);
  ```
  The comment says a new endpoint "should deliver right away"; if that is meant at any hour, the test is exposing a product bug and the owner should decide. Either way the test must not read the clock.

**Confidence:** high. The failure times and the window match exactly.

## 5. Agents edit migrations that are already deployed

**Seen in:** S05, S10 (2 of 16 sessions) · one edit declined, one made and then called out
**What happens:** To add a column or index, the agent edited an existing `migration.sql` under `packages/shared/prisma/migrations/` instead of creating a new one. Prisma checksums applied migrations, so this breaks `prisma migrate deploy` (`Makefile:11`) on staging and prod.
**Evidence:**
- S05: "don't touch that one, it's already been applied on staging and prod. new migration please"
- S10: "again with editing old migrations?? 20261015112400 is deployed, prisma will fail the checksum on staging. new migration. every. time."

**Why it keeps happening:** Not written down and not enforced. `CLAUDE.md` does not mention migrations, and the repository has no `.claude/settings.json`.
**Fix:**
- `CLAUDE.md`, under Conventions:
  ```markdown
  - Never edit an existing file under `packages/shared/prisma/migrations/`: they are deployed and
    Prisma checksums them. Schema changes always get a new migration directory.
  ```
- Enforce it (said twice, so a line alone is not enough). `.claude/hooks/guard-migrations.sh`:
  ```sh
  #!/bin/sh
  # Block edits to a Prisma migration that is already committed; new migration files pass.
  f=$(jq -r '.tool_input.file_path // empty')
  case "$f" in
    */prisma/migrations/*/migration.sql)
      rel=${f#"$CLAUDE_PROJECT_DIR"/}
      if git -C "$CLAUDE_PROJECT_DIR" cat-file -e "HEAD:$rel" 2>/dev/null; then
        echo "Committed migrations are immutable (already deployed). Create a new migration instead." >&2
        exit 2
      fi ;;
  esac
  exit 0
  ```
  `.claude/settings.json`:
  ```json
  {
    "hooks": {
      "PreToolUse": [
        {
          "matcher": "Edit|Write",
          "hooks": [
            { "type": "command", "command": "sh \"$CLAUDE_PROJECT_DIR/.claude/hooks/guard-migrations.sh\"" }
          ]
        }
      ]
    }
  }
  ```
  The script was run here against three paths: the committed `20261002120000_add_endpoint_secret/migration.sql` is blocked (exit 2); a new migration path and `packages/shared/src/retry.ts` pass (exit 0). It needs `jq`. It has not been run inside a live Claude Code session.

**Confidence:** high for the cause; the hook is tested as a script only.

## 6. `pnpm lint` runs longer than the 2-minute tool timeout

**Seen in:** S03, S07 (2 of 16 sessions) · `pnpm lint` was cut off each time
**What happens:** `.eslintrc.cjs:7` sets `project: ['./**/tsconfig.json']` for type-aware linting. That glob is expanded from the repo root and walks every `node_modules` directory, a documented typescript-eslint performance problem, on top of `recommended-type-checked` building a TypeScript program per package.
**Evidence:** `Command timed out after 2m 0.0s` (S03, S07)
**Why it keeps happening:** The default lint command is too slow for an agent's default timeout, and `CLAUDE.md:15` lists it with no hint about duration.
**Fix:**
- `.eslintrc.cjs:7`:
  ```js
  project: ['./packages/*/tsconfig.json'],
  ```
- Until that is measured, `CLAUDE.md:15`:
  ```markdown
  - `pnpm lint` - slow (over 2 minutes): run it with a 5-minute timeout or in the background
  ```

**Confidence:** medium, unconfirmed by measurement. The glob is there and is a known cause, but dependencies are not installed in this checkout so lint was not timed before and after.

## 7. `pnpm typecheck` fails until the Prisma client is regenerated

**Seen in:** S01, S02 (2 of 16 sessions) · `pnpm typecheck` failed, then passed after `pnpm db:generate` with no file edited
**What happens:** `packages/shared/src/db.ts:1` imports `./generated/prisma`, which is git-ignored (`.gitignore:5`) and written only by `prisma generate` (`packages/shared/package.json:10`). On a fresh checkout, or after `schema.prisma` changes (S02 added a column), the client is missing or stale and `tsc` fails in `@tallyhook/shared`.
**Evidence:** `ERR_PNPM_RECURSIVE_RUN_FIRST_FAIL  @tallyhook/shared@0.1.0 typecheck: `tsc --noEmit -p .`` with `Exit status 2` (S01, S02)
**Why it keeps happening:** `CLAUDE.md:12` and `:17` name `pnpm typecheck` as the gate, but nothing says, or makes sure, that generation comes first. No script runs it automatically.
**Fix:**
- `package.json:7`:
  ```json
  "typecheck": "pnpm --filter @tallyhook/shared db:generate && pnpm -r typecheck",
  ```
- `CLAUDE.md`, under Commands:
  ```markdown
  - `pnpm --filter @tallyhook/shared db:generate` - regenerate the Prisma client; needed after any
    change to `schema.prisma` (the client is git-ignored)
  ```

**Confidence:** medium. The mechanism is confirmed in the files; the full tsc output was folded in the evidence, so the exact error text was not seen.

## 8. "Tests passed" was reported when no test had run

**Seen in:** S10, about S09's work (1 report; a second doubt in S15, 2 of 16 sessions)
**What happens:** In S09 the agent filtered the worker tests by a file name that does not exist. `packages/worker/package.json:10` is `vitest run --passWithNoTests`, so vitest printed "No test files found" and exited 0, and the agent reported success. CI then went red on `signature.test.ts`. In S15 the agent reported `/readyz` done from unit tests alone.
**Evidence:**
- S10: "CI on main is red: signature.test.ts > signPayload > is a hex HMAC-SHA256 of the body. you told me tuesday night the signer tests passed - did you actually run them? there is no file called signer, vitest just printed 'No test files found' and exited 0"
- S15: "did you actually hit it though? redis isn't even running on my machine right now so a 200 would be wrong"

**Why it keeps happening:** The flag turns a wrong filter into a green run. The worker has three test files, so the flag protects nothing.
**Fix:**
- `packages/worker/package.json:10`:
  ```json
  "test": "vitest run"
  ```
- `CLAUDE.md`, after line 17:
  ```markdown
  Before saying tests pass, check that the output lists the test files you meant to run.
  ```

**Confidence:** high for the flag (S10 quotes the vitest output); S15 is a related single remark, not a second instance of the same mechanism.

## Looked at, not a problem

- `pnpm install` ECONNRESET (S12): a transient registry failure, once.
- `packages/shared/package.json` edit miss (S07): the agent searched for `"zod": "^3.22.4"`; the file pins `"zod": "3.22.4"`. A one-off.
- `schema.prisma` and `retry.ts` edit-reverts (S14): the human changed the requirement ("actually hold on - keep it at 3").
- Empty searches for `console.` and `no-console` (S14): the human asked for that sweep; finding nothing was the answer. Counted under problem 2.
- Empty searches in S01 and S11: looking for code that did not exist yet.
- S08 interruption of `pnpm dev` ("no need to run it"): once; the foreground rule in problem 1 covers it.
- `.env.example` "probable correction" (S06): the remark was about the `postWebhook` rename, counted under problem 3.
- S01 "make the default limit 25", S04 "ok cool thx", S06 "also add the env var", S10 "second thing: we need an index": new requests or thanks.
- `packages/worker/src/metrics.ts` and `metrics.test.ts` are not in the tree: this checkout predates some sessions (it has 2 migrations; S10 names a third). No conclusion drawn from them.

## Measure it

After the fixes, `/agent-friction:compare` in a week or two shows whether these sessions' failures went down.
